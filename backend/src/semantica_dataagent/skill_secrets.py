"""Runtime access to personal Skill secrets, scoped to one invocation."""

from contextlib import contextmanager

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import text

from .crypto import decrypt
from .db import SkillSecret, sessions
from .skill_catalog import prepare_skill, require_prepared_skill


@contextmanager
def skill_run_secrets(requester, project_id, name):
    values = {}
    try:
        version, snapshot = prepare_skill(requester, project_id, name)
        with sessions().begin() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            require_prepared_skill(db, requester, project_id, name, version)
            rows = (
                db.query(SkillSecret)
                .filter_by(
                    owner_id=requester.id, project_id=project_id, skill_name=name
                )
                .all()
            )
            encrypted = {row.secret_name: row.ciphertext for row in rows}
            if any(key not in encrypted for key in snapshot.secrets):
                raise HTTPException(409, "Required Skill secret missing")
        try:
            values.update({key: decrypt(encrypted[key]) for key in snapshot.secrets})
        except (InvalidToken, ValueError, KeyError, UnicodeError):
            raise HTTPException(503, "Skill secrets unavailable") from None
        yield snapshot, values
    finally:
        values.clear()
