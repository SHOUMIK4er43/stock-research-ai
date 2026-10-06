"""Manual Part 3C proof. Requests and hand-checked expectations are supplied separately."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from stock_research_ai.bootstrap import context_engine
from stock_research_ai.config import Settings
from stock_research_ai.domain.macro import MacroSnapshot
from stock_research_ai.domain.publications import PublicationSnapshot


def check(condition, message):
    if not condition:
        raise ValueError(message)


def cli(database, *args):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    environment["STOCK_RESEARCH_AI_ARTIFACT_PATH"] = str(database.parent / "artifacts")
    result = subprocess.run(
        [sys.executable, "-B", "-m", "stock_research_ai", "--database", str(database), *args],
        capture_output=True,
        text=True,
        env=environment,
    )
    check(result.returncode in (0, 3), f"CLI failed ({result.returncode}); inspect local exports")


def raw_paths(output):
    manifest = json.loads((output / "raw/manifest.json").read_text())
    paths = []
    for artifact in manifest["artifacts"]:
        path = output / manifest["export_paths"][artifact["id"]]
        check(
            hashlib.sha256(path.read_bytes()).hexdigest() == artifact["content_sha256"],
            "Raw SHA mismatch",
        )
        check(artifact["complete"], "Incomplete raw evidence")
        paths.append(path.resolve())
    return paths


async def prove(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    database = root / "proof.sqlite3"
    cli(database, "init-db")
    expected = json.loads(args.expected.read_text())
    results = {}
    for name in ("company", "industry", "macro"):
        request_path = getattr(args, f"{name}_request").resolve()
        request = json.loads(request_path.read_text())
        command = "ingest-macro" if name == "macro" else "ingest-publication"
        output = root / name
        cli(database, command, "--request", str(request_path), "--output", str(output))
        model = MacroSnapshot if name == "macro" else PublicationSnapshot
        snapshot = model.model_validate_json((output / "normalized/snapshot.json").read_text())
        truth = expected[name]
        paths = raw_paths(output)
        if isinstance(snapshot, PublicationSnapshot):
            check(snapshot.identity.publisher == truth["publisher"], f"{name}: publisher")
            check(snapshot.title == truth["title"], f"{name}: title")
            check(str(snapshot.publication_date) == truth["date"], f"{name}: date")
            check(snapshot.reference_period == truth.get("reference_period"), f"{name}: period")
            check(snapshot.retrieval.provider == truth["provider"], f"{name}: provider")
            check(any(truth["passage"] in p.text for p in snapshot.passages), f"{name}: passage")
            check(
                any(s.identifier == truth["subject"] and s.declaration for s in snapshot.subjects),
                f"{name}: declared subject linkage",
            )
            if truth.get("locator"):
                check(
                    any(
                        truth["passage"] in p.text
                        and p.provenance.source_location.locator == truth["locator"]
                        for p in snapshot.passages
                    ),
                    f"{name}: locator",
                )
            request["replay_file"] = str(paths[0])
        else:
            check(snapshot.series is not None, "Missing macro identity")
            for field, value in truth["series"].items():
                check(str(getattr(snapshot.series, field)) == value, f"Macro series: {field}")
            check(
                {o.reference_period: str(o.value) for o in snapshot.observations}
                == truth["values"],
                "Macro numerical golden mismatch; do not change expectations",
            )
            check(
                all(o.source_status == truth["source_status"] for o in snapshot.observations),
                "Source status mismatch",
            )
            request["metadata_file"], request["data_file"] = map(str, paths)
        check(snapshot.coverage.overall == truth["coverage"], f"{name}: coverage")
        replay_request = root / f"{name}-replay.json"
        replay_request.write_text(json.dumps(request, indent=2))
        replay_output = root / f"{name}-replay"
        cli(database, command, "--request", str(replay_request), "--output", str(replay_output))
        replay = model.model_validate_json((replay_output / "normalized/snapshot.json").read_text())
        check(snapshot.id != replay.id, "Replay must create a new immutable snapshot")
        check(
            [p.read_bytes() for p in paths] == [p.read_bytes() for p in raw_paths(replay_output)],
            "Replay evidence changed",
        )
        settings = Settings(database_path=database, artifact_path=root / "artifacts")
        async with context_engine(settings) as engine:
            if isinstance(snapshot, PublicationSnapshot):
                history = await engine.publication_history(snapshot.identity.id)
            else:
                history = await engine.macro_history(snapshot.series.id)
            check(
                len(history) == 2 and {x.id for x in history} == {snapshot.id, replay.id},
                "Persistence/history mismatch",
            )
        results[name] = {
            "golden": "PASS",
            "raw_sha_provenance": "PASS",
            "history": "PASS",
            "coverage": snapshot.coverage.overall,
        }
    (root / "proof.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("company-request", "industry-request", "macro-request", "expected", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    asyncio.run(prove(parser.parse_args()))
