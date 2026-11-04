"""Package entry point."""


def main() -> None:
    from disk_explorer.application import DiskExplorerApp

    DiskExplorerApp().run()


if __name__ == "__main__":
    main()
