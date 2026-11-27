"""Offline smoke run with explicit scripted responses, including a negative control."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from .service import EvaluationService


async def run(args):
    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    service = EvaluationService(str(output_dir / "experiments.sqlite"))
    await service.start()
    try:
        experiment = await service.begin(
            "local-practice",
            mode="offline",
            idempotency_key=str(uuid4()),
            offline_scenario="wrong_total" if args.negative else "correct",
        )
        await asyncio.gather(service.tasks[experiment["id"]])
        experiment = await service.get("local-practice", experiment["id"])
        export = await service.report("local-practice", experiment["id"])
        filename = output_dir / export["filename"]
        filename.write_text(export["content"] + "\n", encoding="utf-8")
        for item in experiment["items"]:
            print(json.dumps({"case_id": item["case_id"], "verdict": item["verdict"], "output": item["output"]}, ensure_ascii=False))
        print(json.dumps({"mode": "offline-scripted-demo", "summary": experiment["summary"], "report": str(filename)}, ensure_ascii=False))
        expected_failed = 1 if args.negative else 0
        return 0 if (experiment["summary"]["failed"] == expected_failed and experiment["summary"]["unscored"] == 0) else 1
    finally:
        await service.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--negative", action="store_true", help="Change scripted total to 999; expect one failed grade")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
