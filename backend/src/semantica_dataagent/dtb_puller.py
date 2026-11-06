"""DTB metadata puller."""

import httpx


class DtbPuller:
    id = "dtb"
    version = "1"
    display_name = "DTB"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "interval_seconds": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    }

    def list_tables(self, client, base_url, token, layer):
        rows = []
        page = 1
        expected = None
        seen = set()
        while True:
            try:
                response = client.get(
                    base_url.rstrip("/") + "/api/data-map/table",
                    params={"page": page, "limit": 500, "layer": layer},
                    headers={
                        "token": token,
                        "x-requested-with": "XMLHttpRequest",
                        "cookie": f"satoken={token}",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("status")) != "200":
                    raise ValueError("DTB table request was rejected")
                data = payload["data"]
                batch = data["tableList"]
                total = data.get("totalElements", data.get("total"))
                if not isinstance(batch, list) or isinstance(total, bool):
                    raise ValueError("Invalid DTB pagination")
                total = int(total)
                if total < 0 or (expected is not None and total != expected):
                    raise ValueError("Inconsistent DTB pagination")
                expected = total
                for row in batch:
                    if not isinstance(row, dict) or "tableId" not in row:
                        raise ValueError("Invalid DTB table")
                    identity = str(row["tableId"])
                    if identity in seen:
                        raise ValueError("Duplicate DTB table")
                    seen.add(identity)
                rows.extend(batch)
                if len(rows) == total:
                    return rows
                if not batch or len(rows) > total:
                    raise ValueError("Incomplete DTB pagination")
                page += 1
            except (httpx.HTTPError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError("DTB table request failed") from exc
