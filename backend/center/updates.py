from pathlib import Path
import json
import re
from fastapi import Depends, HTTPException, Response
from fastapi.responses import FileResponse


def install(app, agent):
    directory = Path(__file__).resolve().parents[2] / ".data/center/releases"

    @app.get("/agent/update/latest")
    def latest(node=Depends(agent)):
        pilot = directory / f"latest-{node}.json"
        try:
            stable = directory / "latest.json"
            selected = pilot if pilot.is_file() else stable
            value = json.loads(selected.read_text(encoding="utf-8"))
            if selected == pilot and stable.is_file():
                shared = json.loads(stable.read_text(encoding="utf-8"))
                if shared.get("release", {}).get("version", "") > value.get("release", {}).get("version", ""):
                    value = shared
            return value
        except FileNotFoundError:
            return Response(status_code=204)

    @app.get("/agent/update/packages/{version}", dependencies=[Depends(agent)])
    def package(version: str):
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", version):
            raise HTTPException(404)
        path = directory / (version + ".zip")
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="application/zip", headers={"Cache-Control": "no-store"})
