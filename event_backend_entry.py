"""Fixed entry point used by the protected Windows backend supervisor."""
if __name__ == '__main__':
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from photo_api.event_backend import main

    main()
