"""Command-line interface for GTNH Updater."""

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from gtnh_updater.updater import GTNHUpdater
from gtnh_updater.state import UpdateState

app = typer.Typer(
    name="gtnh-updater",
    help="Update GT New Horizons installations between versions.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def update(
    old_instance: Annotated[
        Path,
        typer.Option(
            "--old",
            "-o",
            help="Path to the current GTNH .minecraft folder (e.g., .../instances/GTNH_2.8.0/.minecraft)",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    new_zip: Annotated[
        Path,
        typer.Option(
            "--new",
            "-n",
            help="Path to the new GTNH version zip file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output",
            "-O",
            help="Directory where the new instance will be created (e.g., .../instances/)",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    instance_name: Annotated[
        Optional[str],
        typer.Option(
            "--name",
            help="Name for the new instance folder (defaults to zip filename without extension)",
        ),
    ] = None,
) -> None:
    """Update a GTNH installation to a new version.

    This will:
    1. Extract the new version zip to create a fresh instance
    2. Copy your saves, journeymap data, and other user files
    3. Merge your config customizations using git (showing conflicts if any)
    """
    updater = GTNHUpdater(console)

    try:
        updater.update(
            old_instance=old_instance,
            new_zip=new_zip,
            output_dir=output_dir,
            instance_name=instance_name,
        )
    except updater.ConfigConflictError as e:
        console.print()
        console.print("[bold red]Config merge conflicts detected![/bold red]")
        console.print()
        console.print("The following files have conflicts that need manual resolution:")
        console.print()
        for conflict_file in e.conflicts:
            console.print(f"  [yellow]•[/yellow] {conflict_file}")
        console.print()
        console.print(f"[bold]Config repository location:[/bold] {e.config_repo_path}")
        console.print()
        console.print("To resolve:")
        console.print("  1. Open each conflicting file and resolve the merge markers")
        console.print("     (look for <<<<<<< HEAD, =======, and >>>>>>> sections)")
        console.print("  2. Stage your resolved files: [cyan]git add <file>[/cyan]")
        console.print("  3. Complete the merge: [cyan]git commit[/cyan]")
        console.print(f"  4. Run: [cyan]gtnh-updater resume --state {e.state_file}[/cyan]")
        console.print()
        raise typer.Exit(1)


@app.command()
def resume(
    state_file: Annotated[
        Optional[Path],
        typer.Option(
            "--state",
            "-s",
            help="Path to the state file from a previous interrupted update",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Resume an interrupted update after resolving config conflicts.

    If no state file is provided, looks for the most recent state file
    in the default cache directory.
    """
    updater = GTNHUpdater(console)

    if state_file is None:
        state_file = UpdateState.find_latest_state_file()
        if state_file is None:
            console.print("[red]No state file found.[/red]")
            console.print("Please provide a state file with --state, or start a new update.")
            raise typer.Exit(1)
        console.print(f"[dim]Found state file: {state_file}[/dim]")

    try:
        updater.resume(state_file)
    except updater.ConfigConflictError as e:
        console.print()
        console.print("[bold red]Config conflicts still exist![/bold red]")
        console.print()
        console.print("The following files still have unresolved conflicts:")
        console.print()
        for conflict_file in e.conflicts:
            console.print(f"  [yellow]•[/yellow] {conflict_file}")
        console.print()
        console.print("Please resolve these conflicts before running resume again.")
        raise typer.Exit(1)


@app.command()
def status(
    state_file: Annotated[
        Optional[Path],
        typer.Option(
            "--state",
            "-s",
            help="Path to the state file to check",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Check the status of an in-progress update."""
    if state_file is None:
        state_file = UpdateState.find_latest_state_file()
        if state_file is None:
            console.print("[green]No in-progress updates found.[/green]")
            return

    state = UpdateState.load(state_file)
    console.print(f"[bold]Update Status[/bold]")
    console.print(f"  Old instance: {state.old_instance}")
    console.print(f"  New instance: {state.new_instance}")
    console.print(f"  Stage: {state.stage}")

    if state.conflicts:
        console.print()
        console.print("[yellow]Unresolved conflicts:[/yellow]")
        for conflict in state.conflicts:
            console.print(f"  • {conflict}")


if __name__ == "__main__":
    app()
