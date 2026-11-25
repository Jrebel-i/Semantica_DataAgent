"""Runtime access to personal Skill secrets, scoped to one invocation."""

from contextlib import ExitStack, contextmanager

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import text

from .crypto import decrypt
from .db import SkillSecret, sessions
from .skill_catalog import (
    prepare_project_skills,
    prepare_skill,
    require_prepared_project_skills,
    require_prepared_skill,
)


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


@contextmanager
def project_skill_run_secrets(
    requester, project_id: str, explicit_skill_name: str | None = None
):
    """Mount all authorized enabled upload Skills and inject ready personal secrets."""
    merged_secrets: dict[str, str] = {}
    with ExitStack() as stack:
        try:
            explicit_snapshot = None
            explicit_secrets: dict[str, str] = {}
            if explicit_skill_name is not None:
                explicit_snapshot, explicit_secrets = stack.enter_context(
                    skill_run_secrets(requester, project_id, explicit_skill_name)
                )
            prepared_meta, candidates = prepare_project_skills(requester, project_id)
            encrypted_by_skill: dict[str, dict[str, str]] = {}
            valid_names: set[str] = set()
            if candidates:
                with sessions().begin() as db:
                    db.execute(text("BEGIN IMMEDIATE"))
                    valid_names = require_prepared_project_skills(
                        db, requester, project_id, prepared_meta
                    )
                    candidate_names = [
                        snap.name for snap in candidates if snap.name in valid_names
                    ]
                    if candidate_names:
                        rows = (
                            db.query(SkillSecret)
                            .filter(
                                SkillSecret.owner_id == requester.id,
                                SkillSecret.project_id == project_id,
                                SkillSecret.skill_name.in_(candidate_names),
                            )
                            .all()
                        )
                        for row in rows:
                            encrypted_by_skill.setdefault(row.skill_name, {})[
                                row.secret_name
                            ] = row.ciphertext
            mounted_by_name = {
                snap.name: snap for snap in candidates if snap.name in valid_names
            }
            if explicit_snapshot is not None and explicit_snapshot.source == "upload":
                mounted_by_name[explicit_snapshot.name] = explicit_snapshot
            mounted_snapshots = tuple(
                mounted_by_name[name] for name in sorted(mounted_by_name)
            )
            merged_secrets.update(explicit_secrets)
            for snap in mounted_snapshots:
                if (
                    explicit_snapshot is not None
                    and snap.name == explicit_snapshot.name
                ):
                    continue
                if not snap.secrets:
                    continue
                encrypted = encrypted_by_skill.get(snap.name, {})
                if any(key not in encrypted for key in snap.secrets):
                    continue
                try:
                    decrypted = {key: decrypt(encrypted[key]) for key in snap.secrets}
                except (InvalidToken, ValueError, KeyError, UnicodeError):
                    continue
                for key, val in decrypted.items():
                    if key not in merged_secrets:
                        merged_secrets[key] = val
            yield explicit_snapshot, mounted_snapshots, merged_secrets
        finally:
            merged_secrets.clear()
