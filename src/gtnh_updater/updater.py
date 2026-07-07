"""Core updater logic for GTNH version updates."""

import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from gtnh_updater.state import UpdateState


class GTNHUpdater:
    """Handles the GTNH update process."""

    BASELINE_BRANCH = "version-baseline"
    NEW_VERSION_BRANCH = "new-version"

    # User data files/folders to copy from old instance to new instance
    # Based on GTNH wiki migration guide
    USER_DATA_ITEMS = [
        # Folders
        ("saves", True),  # (name, is_directory)
        ("backups", True),
        ("journeymap", True),
        ("visualprospecting", True),
        ("TCNodeTracker", True),
        ("schematics", True),
        ("resourcepacks", True),
        ("shaderpacks", True),
        ("screenshots", True),
        # Files
        ("localconfig.cfg", False),
        ("BotaniaVars.dat", False),
        ("options.txt", False),
        ("optionsnf.txt", False),
        ("servers.dat", False),
    ]

    # Config file that needs special handling (copied alongside shaderpacks)
    SHADER_CONFIG = "config/shaders.properties"

    # Config folders to merge (preserves user customizations while adding new options)
    CONFIG_FOLDERS_TO_MERGE = [
        "config",
        "serverutilities",
    ]

    class ConfigConflictError(Exception):
        """Raised when config merge has unresolved conflicts."""

        def __init__(self, conflicts: list[str], config_repo_path: Path, state_file: Path):
            self.conflicts = conflicts
            self.config_repo_path = config_repo_path
            self.state_file = state_file
            super().__init__(f"Config merge conflicts in {len(conflicts)} file(s)")

    class UpdateError(Exception):
        """General update error with helpful message."""

        pass

    def __init__(self, console: Console):
        self.console = console

    def update(
        self,
        old_instance: Path,
        new_zip: Path,
        output_dir: Path,
        instance_name: Optional[str] = None,
    ) -> None:
        """Perform the full update process."""
        # Validate old instance is a .minecraft folder
        if not self._is_valid_minecraft_dir(old_instance):
            raise self.UpdateError(
                f"'{old_instance}' does not appear to be a valid .minecraft directory.\n"
                f"Expected to find 'mods' and 'config' folders inside.\n"
                f"Make sure you're pointing to the .minecraft folder, not the instance folder."
            )

        # Determine instance name from zip if not provided
        if instance_name is None:
            instance_name = new_zip.stem  # Filename without extension

        new_instance = output_dir / instance_name

        # Check if target already exists
        if new_instance.exists():
            raise self.UpdateError(
                f"Target instance already exists: {new_instance}\n"
                f"Please remove it first or choose a different name with --name."
            )

        # Initialize state for resume capability
        state = UpdateState(
            old_instance=str(old_instance),
            new_instance=str(new_instance),
            new_zip=str(new_zip),
            output_dir=str(output_dir),
            stage="extracting",
        )

        self.console.print(f"[bold]GTNH Version Updater[/bold]")
        self.console.print(f"  Old instance: {old_instance}")
        self.console.print(f"  New version:  {new_zip.name}")
        self.console.print(f"  Output:       {new_instance}")
        self.console.print()

        # Step 1: Extract new version
        self.console.print("[bold cyan]Step 1/4:[/bold cyan] Extracting new version...")
        minecraft_dir = self._extract_new_version(new_zip, new_instance)
        state.stage = "extracted"
        state.new_instance = str(minecraft_dir)  # Update to actual .minecraft path

        # Step 2: Copy user data
        self.console.print("[bold cyan]Step 2/4:[/bold cyan] Copying user data...")
        state.stage = "copying_user_data"
        self._copy_user_data(old_instance, minecraft_dir)

        # Step 3: Handle configs
        self.console.print("[bold cyan]Step 3/4:[/bold cyan] Merging configs...")
        state.stage = "merging_configs"
        state_file = state.save()

        try:
            self._merge_configs(old_instance, minecraft_dir, state)
        except self.ConfigConflictError as e:
            # Update state with conflict info
            state.conflicts = e.conflicts
            state.config_repo_path = str(e.config_repo_path)
            state.save()
            raise self.ConfigConflictError(
                conflicts=e.conflicts,
                config_repo_path=e.config_repo_path,
                state_file=state_file,
            )

        # Step 4: Finalize
        self.console.print("[bold cyan]Step 4/4:[/bold cyan] Finalizing...")
        state.stage = "completed"
        state.clear()

        self.console.print()
        self.console.print("[bold green]Update complete![/bold green]")
        self.console.print(f"New instance created at: {new_instance}")
        self.console.print()
        self.console.print("[dim]Remember to verify your instance in Prism/MultiMC before deleting the old one.[/dim]")

    def resume(self, state_file: Path) -> None:
        """Resume an interrupted update."""
        state = UpdateState.load(state_file)

        self.console.print(f"[bold]Resuming GTNH update...[/bold]")
        self.console.print(f"  Stage: {state.stage}")
        self.console.print()

        minecraft_dir = Path(state.new_instance)
        old_instance = Path(state.old_instance)

        if state.stage == "merging_configs" and state.conflicts:
            # Check if conflicts are resolved
            config_repo = Path(state.config_repo_path) if state.config_repo_path else None

            if config_repo and config_repo.exists():
                # Check for remaining conflicts
                conflicts = self._get_git_conflicts(config_repo)

                if conflicts:
                    raise self.ConfigConflictError(
                        conflicts=conflicts,
                        config_repo_path=config_repo,
                        state_file=state_file,
                    )

                # Conflicts resolved, continue with the merge
                self.console.print("[green]Conflicts resolved![/green] Continuing update...")
                self._finalize_new_version_branch(config_repo)
                self._finish_config_merge(config_repo, minecraft_dir)

        # Finalize
        state.stage = "completed"
        state.clear()

        self.console.print()
        self.console.print("[bold green]Update complete![/bold green]")
        self.console.print(f"Instance ready at: {minecraft_dir.parent}")

    def _is_valid_minecraft_dir(self, path: Path) -> bool:
        """Check if the path looks like a valid .minecraft directory."""
        return (path / "mods").exists() or (path / "config").exists()

    def _extract_new_version(self, zip_path: Path, output_dir: Path) -> Path:
        """Extract the new version zip and return the path to the .minecraft folder."""
        import tempfile

        # Extract to a temp directory first to handle nested folder structure
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            with zipfile.ZipFile(zip_path, "r") as zf:
                members = zf.namelist()
                total = len(members)

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    console=self.console,
                ) as progress:
                    task = progress.add_task("Extracting...", total=total)

                    for member in members:
                        zf.extract(member, temp_path)
                        progress.advance(task)

            # Find the actual instance folder (the one containing .minecraft)
            instance_folder = self._find_instance_folder(temp_path)

            if instance_folder is None:
                raise self.UpdateError(
                    f"Could not find instance folder in extracted archive.\n"
                    f"Expected to find a folder containing .minecraft, libraries, etc.\n"
                    f"The zip file structure may be unexpected."
                )

            # Move the instance folder to the output directory
            shutil.move(str(instance_folder), str(output_dir))

        # Now find the .minecraft folder in the output
        minecraft_dir = self._find_minecraft_dir(output_dir)

        if minecraft_dir is None:
            raise self.UpdateError(
                f"Could not find .minecraft folder in extracted archive.\n"
                f"The zip file structure may be unexpected.\n"
                f"Extracted to: {output_dir}"
            )

        self.console.print(f"  [dim]Extracted to: {output_dir}[/dim]")
        return minecraft_dir

    def _find_instance_folder(self, base_path: Path) -> Optional[Path]:
        """Find the instance folder (containing .minecraft) in extracted archive."""
        # Check if base_path itself has .minecraft
        if (base_path / ".minecraft").exists():
            return base_path

        # Check one level deep - zips usually have a top-level folder
        for child in base_path.iterdir():
            if child.is_dir() and (child / ".minecraft").exists():
                return child

        return None

    def _find_minecraft_dir(self, base_path: Path) -> Optional[Path]:
        """Find the .minecraft folder in the extracted archive."""
        # Check if base_path itself is the minecraft dir
        if self._is_valid_minecraft_dir(base_path):
            return base_path

        # Look for .minecraft folder
        minecraft_path = base_path / ".minecraft"
        if minecraft_path.exists() and self._is_valid_minecraft_dir(minecraft_path):
            return minecraft_path

        # Check one level deep for any folder containing mods/config
        for child in base_path.iterdir():
            if child.is_dir():
                if self._is_valid_minecraft_dir(child):
                    return child
                # Check for .minecraft inside
                nested = child / ".minecraft"
                if nested.exists() and self._is_valid_minecraft_dir(nested):
                    return nested

        return None

    def _copy_user_data(self, old_instance: Path, new_instance: Path) -> None:
        """Copy user data from old instance to new instance."""
        copied_items = []
        skipped_items = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
        ) as progress:
            task = progress.add_task("Copying user data...", total=len(self.USER_DATA_ITEMS) + 1)

            for item_name, is_dir in self.USER_DATA_ITEMS:
                source = old_instance / item_name
                dest = new_instance / item_name

                if source.exists():
                    if is_dir:
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(source, dest)
                    else:
                        shutil.copy2(source, dest)
                    copied_items.append(item_name)
                else:
                    skipped_items.append(item_name)

                progress.advance(task)

            # Handle shader config specially
            shader_config_src = old_instance / self.SHADER_CONFIG
            shader_config_dst = new_instance / self.SHADER_CONFIG
            if shader_config_src.exists():
                shader_config_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(shader_config_src, shader_config_dst)
                copied_items.append(self.SHADER_CONFIG)

            progress.advance(task)

        if copied_items:
            self.console.print(f"  [green]✓[/green] Copied {len(copied_items)} items:")
            for item in copied_items:
                self.console.print(f"    • {item}")

    def _merge_configs(self, old_instance: Path, new_instance: Path, state: UpdateState) -> None:
        """Merge configs using git to preserve user customizations.

        If the old instance has an existing config repo (from a previous update),
        we use that to preserve the full history of customizations. Otherwise,
        we create a fresh repo.
        """
        # Check if git is available
        if not self._is_git_installed():
            self.console.print(
                "[yellow]Warning:[/yellow] Git is not installed. "
                "Configs will be replaced entirely (your customizations will be lost)."
            )
            self.console.print(
                "[dim]Install git for smart config merging that preserves your changes.[/dim]"
            )
            # Just use new configs as-is
            return

        config_repo = new_instance / ".updater_config_repo"
        old_config_repo = old_instance / ".updater_config_repo"

        # Check if old instance has an existing config repo from previous updates
        if old_config_repo.exists() and (old_config_repo / ".git").exists():
            self.console.print("  [dim]Found existing config history, using it for merge...[/dim]")
            # Copy the existing repo to the new instance
            shutil.copytree(old_config_repo, config_repo)
            self._run_git(config_repo, ["checkout", "old-version"])
            # Update with current user configs (in case they made changes since last update)
            self._update_config_repo_with_current(config_repo, old_instance)
            # Add new version configs and merge
            self._add_new_version_and_merge(config_repo, new_instance, state)
            # Copy merged configs back to new instance
            self._finish_config_merge(config_repo, new_instance)
        else:
            # Fresh start - create new repo
            self._init_config_repo(config_repo, new_instance)
            self._setup_old_config_branch(config_repo, old_instance)
            conflicts = self._attempt_config_merge(config_repo)

            if conflicts:
                state.config_repo_path = str(config_repo)
                raise self.ConfigConflictError(
                    conflicts=conflicts,
                    config_repo_path=config_repo,
                    state_file=Path(""),  # Will be set by caller
                )

            # No conflicts, copy merged configs back
            self._finish_config_merge(config_repo, new_instance)

    def _update_config_repo_with_current(self, repo_path: Path, old_instance: Path) -> None:
        """Update the config repo with the user's current config state."""
        # Copy all config folders from old instance
        for folder in self.CONFIG_FOLDERS_TO_MERGE:
            source = old_instance / folder
            dest = repo_path / folder
            if dest.exists():
                shutil.rmtree(dest)
            if source.exists():
                shutil.copytree(source, dest)

        # Commit any changes the user made since last update
        self._run_git(repo_path, ["add", "."])
        try:
            self._run_git(repo_path, ["commit", "-m", "User config changes since last update"])
        except subprocess.CalledProcessError:
            # No changes to commit, that's fine
            pass

    def _add_new_version_and_merge(self, repo_path: Path, new_instance: Path, state: UpdateState) -> None:
        """Add new version configs to repo and merge."""
        baseline_commit = self._find_version_baseline(repo_path)

        self._run_git(repo_path, ["checkout", "old-version"])
        self._delete_branch_if_exists(repo_path, self.NEW_VERSION_BRANCH)
        self._run_git(repo_path, ["checkout", "-b", self.NEW_VERSION_BRANCH, baseline_commit])

        # Replace all config folders with new version
        for folder in self.CONFIG_FOLDERS_TO_MERGE:
            source = new_instance / folder
            dest = repo_path / folder
            if dest.exists():
                shutil.rmtree(dest)
            if source.exists():
                shutil.copytree(source, dest)

        self._run_git(repo_path, ["add", "."])
        try:
            self._run_git(repo_path, ["commit", "-m", "New version configs"])
        except subprocess.CalledProcessError:
            # The new pack has the same configs as the previous baseline.
            pass

        new_version_commit = self._run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()

        # Switch back to main branch and merge
        self._run_git(repo_path, ["checkout", "old-version"])

        try:
            self._run_git(
                repo_path,
                [
                    "merge", self.NEW_VERSION_BRANCH,
                    "-m", "Merge new version configs",
                    "--no-edit",
                    "--no-ff",
                    "--allow-unrelated-histories",
                    "-X", "ours",
                ],
            )
        except subprocess.CalledProcessError:
            # Merge conflict
            conflicts = self._get_git_conflicts(repo_path)
            if conflicts:
                state.config_repo_path = str(repo_path)
                raise self.ConfigConflictError(
                    conflicts=conflicts,
                    config_repo_path=repo_path,
                    state_file=Path(""),
                )

        if self._apply_new_scalar_config_changes(repo_path, baseline_commit, new_version_commit):
            self._run_git(repo_path, ["add", "."])
            self._run_git(repo_path, ["commit", "--amend", "--no-edit"])

        self._record_version_baseline(repo_path, new_version_commit)
        self._run_git(repo_path, ["branch", "-d", self.NEW_VERSION_BRANCH])

    def _apply_new_scalar_config_changes(
        self,
        repo_path: Path,
        baseline_commit: str,
        new_version_commit: str,
    ) -> bool:
        """Apply non-user-edited scalar config changes that Git dropped from conflict hunks."""
        changed_files = self._run_git(
            repo_path,
            [
                "diff",
                "--name-only",
                baseline_commit,
                new_version_commit,
                "--",
                *self.CONFIG_FOLDERS_TO_MERGE,
            ],
        ).stdout.splitlines()

        changed = False
        for relative_file in changed_files:
            merged_file = repo_path / relative_file
            if not merged_file.is_file():
                continue

            baseline_text = self._git_show_text(repo_path, baseline_commit, relative_file)
            new_text = self._git_show_text(repo_path, new_version_commit, relative_file)
            if baseline_text is None or new_text is None:
                continue

            try:
                merged_text = merged_file.read_text()
            except UnicodeDecodeError:
                continue

            updated_text = self._merge_scalar_config_text(baseline_text, new_text, merged_text)
            if updated_text != merged_text:
                merged_file.write_text(updated_text)
                changed = True

        return changed

    def _git_show_text(self, repo_path: Path, commit: str, relative_file: str) -> Optional[str]:
        try:
            return self._run_git(repo_path, ["show", f"{commit}:{relative_file}"]).stdout
        except subprocess.CalledProcessError:
            return None

    def _merge_scalar_config_text(self, baseline_text: str, new_text: str, merged_text: str) -> str:
        baseline = self._parse_scalar_config(baseline_text)
        new = self._parse_scalar_config(new_text)
        merged = self._parse_scalar_config(merged_text)

        merged_lines = merged_text.splitlines(keepends=True)
        replacements: dict[int, str] = {}
        insertions: dict[tuple[str, ...], list[str]] = {}

        for key, new_setting in new["settings"].items():
            baseline_setting = baseline["settings"].get(key)
            merged_setting = merged["settings"].get(key)

            if baseline_setting and merged_setting:
                baseline_value = baseline_setting["value"]
                new_value = new_setting["value"]
                merged_value = merged_setting["value"]
                if new_value != baseline_value and merged_value == baseline_value:
                    replacements[merged_setting["line_index"]] = new_setting["line"]
            elif baseline_setting is None and merged_setting is None:
                section = key[0]
                insertions.setdefault(section, []).append(new_setting["line"])

        for line_index, line in replacements.items():
            merged_lines[line_index] = line

        section_close = merged["section_close"]
        for section, lines in sorted(
            insertions.items(),
            key=lambda item: section_close.get(item[0], len(merged_lines)),
            reverse=True,
        ):
            insert_at = section_close.get(section, len(merged_lines))
            merged_lines[insert_at:insert_at] = lines

        return "".join(merged_lines)

    def _parse_scalar_config(self, text: str) -> dict:
        settings = {}
        section_close = {}
        section_stack: list[str] = []
        setting_pattern = re.compile(r"^([A-Z]):(.+?)=(.*)$")

        for line_index, line in enumerate(text.splitlines(keepends=True)):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if stripped.endswith("{"):
                section_stack.append(stripped[:-1].strip())
                continue

            if stripped == "}":
                section_close[tuple(section_stack)] = line_index
                if section_stack:
                    section_stack.pop()
                continue

            match = setting_pattern.match(stripped)
            if not match:
                continue

            key = (tuple(section_stack), match.group(1), match.group(2).strip())
            settings[key] = {
                "value": match.group(3).strip(),
                "line_index": line_index,
                "line": line,
            }

        return {"settings": settings, "section_close": section_close}

    def _find_version_baseline(self, repo_path: Path) -> str:
        """Find the previous pack-default config commit for a real 3-way merge."""
        try:
            return self._run_git(
                repo_path,
                ["rev-parse", "--verify", f"refs/heads/{self.BASELINE_BRANCH}"],
            ).stdout.strip()
        except subprocess.CalledProcessError:
            pass

        try:
            result = self._run_git(repo_path, ["log", "--all", "--format=%H%x00%s"])
            for line in result.stdout.splitlines():
                commit, _, subject = line.partition("\x00")
                if subject == "New version configs":
                    return commit
        except subprocess.CalledProcessError:
            pass

        try:
            merge_commit = self._run_git(
                repo_path,
                ["rev-list", "--min-parents=2", "--max-count=1", "HEAD"],
            ).stdout.strip()
            if merge_commit:
                return self._run_git(repo_path, ["rev-parse", f"{merge_commit}^2"]).stdout.strip()
        except subprocess.CalledProcessError:
            pass

        return self._run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()

    def _record_version_baseline(self, repo_path: Path, commit: str) -> None:
        """Remember the pack-default config commit for the next update."""
        self._run_git(repo_path, ["branch", "-f", self.BASELINE_BRANCH, commit])

    def _finalize_new_version_branch(self, repo_path: Path) -> None:
        try:
            commit = self._run_git(repo_path, ["rev-parse", self.NEW_VERSION_BRANCH]).stdout.strip()
        except subprocess.CalledProcessError:
            return

        self._record_version_baseline(repo_path, commit)
        self._run_git(repo_path, ["branch", "-D", self.NEW_VERSION_BRANCH])

    def _delete_branch_if_exists(self, repo_path: Path, branch: str) -> None:
        try:
            self._run_git(repo_path, ["rev-parse", "--verify", f"refs/heads/{branch}"])
        except subprocess.CalledProcessError:
            return
        self._run_git(repo_path, ["branch", "-D", branch])

    def _is_git_installed(self) -> bool:
        """Check if git is available."""
        try:
            # On Windows, use CREATE_NO_WINDOW to prevent console flash
            kwargs = {}
            if subprocess.sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(
                ["git", "--version"],
                capture_output=True,
                check=True,
                **kwargs,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _init_config_repo(self, repo_path: Path, new_instance: Path) -> None:
        """Initialize a git repo with the new configs."""
        repo_path.mkdir(parents=True, exist_ok=True)

        # Copy all config folders from new instance to repo
        for folder in self.CONFIG_FOLDERS_TO_MERGE:
            source = new_instance / folder
            dest = repo_path / folder
            if source.exists():
                shutil.copytree(source, dest)

        # Initialize git
        self._run_git(repo_path, ["init"])
        self._run_git(repo_path, ["config", "user.name", "GTNH Updater"])
        self._run_git(repo_path, ["config", "user.email", "updater@localhost"])
        self._run_git(repo_path, ["add", "."])
        self._run_git(repo_path, ["commit", "-m", "New version configs"])
        self._run_git(repo_path, ["branch", "-M", self.NEW_VERSION_BRANCH])

    def _setup_old_config_branch(self, repo_path: Path, old_instance: Path) -> None:
        """Create a branch with old configs for merging."""
        # Create orphan branch for old configs
        self._run_git(repo_path, ["checkout", "--orphan", "old-version"])

        # Remove staged files
        self._run_git(repo_path, ["rm", "-rf", "--cached", "."])

        # Remove config folders and replace with old instance's configs
        for folder in self.CONFIG_FOLDERS_TO_MERGE:
            source = old_instance / folder
            dest = repo_path / folder
            if dest.exists():
                shutil.rmtree(dest)
            if source.exists():
                shutil.copytree(source, dest)

        self._run_git(repo_path, ["add", "."])
        self._run_git(repo_path, ["commit", "-m", "Old version configs with user customizations"])

    def _attempt_config_merge(self, repo_path: Path) -> list[str]:
        """Attempt to merge new configs into old configs. Returns list of conflicting files."""
        try:
            # Merge new-version into old-version (current branch)
            # --allow-unrelated-histories: needed because we use orphan branches
            # -X ours: when there's a conflict, prefer the user's customizations
            #          (new-only files are still added, but existing files keep
            #          user changes; if new version requires specific config
            #          changes, they'll be in release notes)
            self._run_git(
                repo_path,
                [
                    "merge", self.NEW_VERSION_BRANCH,
                    "-m", "Merge new version configs",
                    "--no-edit",
                    "--allow-unrelated-histories",
                    "-X", "ours",
                ],
            )
            self._finalize_new_version_branch(repo_path)
            return []
        except subprocess.CalledProcessError:
            # Merge conflict - get list of conflicting files
            return self._get_git_conflicts(repo_path)

    def _get_git_conflicts(self, repo_path: Path) -> list[str]:
        """Get list of files with merge conflicts."""
        # On Windows, use CREATE_NO_WINDOW to prevent console flash
        kwargs = {}
        if subprocess.sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "--name-only", "--diff-filter=U"],
                capture_output=True,
                text=True,
                check=True,
                **kwargs,
            )
            conflicts = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            return conflicts
        except subprocess.CalledProcessError:
            # Try alternative method
            try:
                result = subprocess.run(
                    ["git", "-C", str(repo_path), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    check=True,
                    **kwargs,
                )
                conflicts = []
                for line in result.stdout.strip().split("\n"):
                    if line.startswith("UU ") or line.startswith("AA ") or line.startswith("DD "):
                        conflicts.append(line[3:].strip())
                return conflicts
            except subprocess.CalledProcessError:
                return []

    def _finish_config_merge(self, repo_path: Path, new_instance: Path) -> None:
        """Copy merged configs back to the new instance.

        The config repo is kept for future updates - it maintains the history
        of user customizations so subsequent version updates can do proper
        3-way merges.
        """
        # Copy all merged config folders back to new instance
        for folder in self.CONFIG_FOLDERS_TO_MERGE:
            merged = repo_path / folder
            dest = new_instance / folder
            if merged.exists():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(merged, dest)

        self.console.print("  [green]✓[/green] Configs merged successfully")

    def _run_git(self, repo_path: Path, args: list[str]) -> subprocess.CompletedProcess:
        """Run a git command in the specified repo."""
        cmd = ["git", "-C", str(repo_path)] + args
        # On Windows, use CREATE_NO_WINDOW to prevent console flash
        kwargs = {}
        if subprocess.sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            **kwargs,
        )
