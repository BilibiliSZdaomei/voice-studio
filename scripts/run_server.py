"""Entry point that works without pip install or an activated environment."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))


def main():
    if "--print-config" in sys.argv or "--diagnose" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--config")
        parser.add_argument("--print-config", action="store_true")
        parser.add_argument("--diagnose", action="store_true")
        args = parser.parse_args()
        from voice_studio.config import load_config, voice_resource_checks
        config = load_config(args.config)
        if args.print_config:
            print(json.dumps(config, ensure_ascii=False))
        else:
            import shutil
            from voice_studio.prepare import RewriteClient
            from voice_studio.video import VideoExporter
            checks = voice_resource_checks(config)
            sample = config.get("accepted_sample") or ""
            ffmpeg = shutil.which("ffmpeg")
            print(json.dumps({"project": str(ROOT), "data_root": config["data_root"],
                              "python": sys.version.split()[0], "ffmpeg": ffmpeg,
                              "checks": {**checks, "accepted_sample": {"path": sample, "exists": Path(sample).is_file()}},
                              "voice_ready": all(item["exists"] for item in checks.values()),
                              "mp3_export_available": bool(ffmpeg),
                              "rewrite": RewriteClient(config).availability(),
                              "video": VideoExporter(config, Path(config["data_root"])).availability()},
                             ensure_ascii=False, indent=2))
        return
    from voice_studio.server import main as run
    run()


if __name__ == "__main__":
    from voice_studio.errors import AppError
    try:
        main()
    except (AppError, ValueError, OSError) as error:
        print(f"声稿：{error}", file=sys.stderr)
        sys.exit(1)
