"""Core updater logic for GTNH version updates."""

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
        old_config = old_instance / "config"
        new_config = new_instance / "config"

        # Check if old instance has an existing config repo from previous updates
        if old_config_repo.exists() and (old_config_repo / ".git").exists():
            self.console.print("  [dim]Found existing config history, using it for merge...[/dim]")
            # Copy the existing repo to the new instance
            shutil.copytree(old_config_repo, config_repo)
            # Update with current user configs (in case they made changes since last update)
            self._update_config_repo_with_current(config_repo, old_config)
            # Add new version configs and merge
            self._add_new_version_and_merge(config_repo, new_config, state)
            # Copy merged configs back to new instance
            self._finish_config_merge(config_repo, new_instance)
        else:
            # Fresh start - create new repo
            self._init_config_repo(config_repo, new_config)
            self._setup_old_config_branch(config_repo, old_config)
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

    def _update_config_repo_with_current(self, repo_path: Path, current_config: Path) -> None:
        """Update the config repo with the user's current config state."""
        config_dest = repo_path / "config"

        # Remove old configs and copy current ones
        if config_dest.exists():
            shutil.rmtree(config_dest)
        shutil.copytree(current_config, config_dest)

        # Commit any changes the user made since last update
        self._run_git(repo_path, ["add", "."])
        try:
            self._run_git(repo_path, ["commit", "-m", "User config changes since last update"])
        except subprocess.CalledProcessError:
            # No changes to commit, that's fine
            pass

    def _add_new_version_and_merge(self, repo_path: Path, new_config: Path, state: UpdateState) -> None:
        """Add new version configs to repo and merge."""
        config_dest = repo_path / "config"

        # Create a branch for the new version
        self._run_git(repo_path, ["checkout", "-b", "new-version"])

        # Replace configs with new version
        shutil.rmtree(config_dest)
        shutil.copytree(new_config, config_dest)

        self._run_git(repo_path, ["add", "."])
        self._run_git(repo_path, ["commit", "-m", "New version configs"])

        # Switch back to main branch and merge
        self._run_git(repo_path, ["checkout", "old-version"])

        try:
            self._run_git(
                repo_path,
                [
                    "merge", "new-version",
                    "-m", "Merge new version configs",
                    "--no-edit",
                    "--allow-unrelated-histories",
                    "-X", "theirs",
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

        # Clean up new-version branch
        self._run_git(repo_path, ["branch", "-d", "new-version"])

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

    def _init_config_repo(self, repo_path: Path, new_config: Path) -> None:
        """Initialize a git repo with the new configs."""
        repo_path.mkdir(parents=True, exist_ok=True)

        # Copy new configs to repo
        config_dest = repo_path / "config"
        shutil.copytree(new_config, config_dest)

        # Initialize git
        self._run_git(repo_path, ["init"])
        self._run_git(repo_path, ["config", "user.name", "GTNH Updater"])
        self._run_git(repo_path, ["config", "user.email", "updater@localhost"])
        self._run_git(repo_path, ["add", "."])
        self._run_git(repo_path, ["commit", "-m", "New version configs"])
        self._run_git(repo_path, ["branch", "-M", "new-version"])

    def _setup_old_config_branch(self, repo_path: Path, old_config: Path) -> None:
        """Create a branch with old configs for merging."""
        config_dest = repo_path / "config"

        # Create orphan branch for old configs
        self._run_git(repo_path, ["checkout", "--orphan", "old-version"])

        # Remove staged files
        self._run_git(repo_path, ["rm", "-rf", "--cached", "."])

        # Remove config dir and replace with old configs
        shutil.rmtree(config_dest)
        shutil.copytree(old_config, config_dest)

        self._run_git(repo_path, ["add", "."])
        self._run_git(repo_path, ["commit", "-m", "Old version configs with user customizations"])

    def _attempt_config_merge(self, repo_path: Path) -> list[str]:
        """Attempt to merge new configs into old configs. Returns list of conflicting files."""
        try:
            # Merge new-version into old-version (current branch)
            # --allow-unrelated-histories: needed because we use orphan branches
            # -X theirs: when there's a conflict, prefer the new version
            #            (user can always re-apply their changes, but new config
            #            changes are likely important for the new version to work)
            self._run_git(
                repo_path,
                [
                    "merge", "new-version",
                    "-m", "Merge new version configs",
                    "--no-edit",
                    "--allow-unrelated-histories",
                    "-X", "theirs",
                ],
            )
            # Clean up new-version branch
            self._run_git(repo_path, ["branch", "-d", "new-version"])
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
        merged_config = repo_path / "config"
        dest_config = new_instance / "config"

        # Remove new instance's config and replace with merged
        if dest_config.exists():
            shutil.rmtree(dest_config)
        shutil.copytree(merged_config, dest_config)

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
