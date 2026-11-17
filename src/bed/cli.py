import argparse
from pathlib import Path

from bed.core.config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bed")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        load_config(args.path.read_text())
    except OSError as e:
        print(f"error: {e}")
        return 1
    except ConfigError as e:
        for error in e.errors:
            print(f"error: {error}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
