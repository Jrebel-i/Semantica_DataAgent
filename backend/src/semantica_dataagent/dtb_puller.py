"""DTB metadata puller."""

from urllib.parse import quote

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

    @staticmethod
    def _get(client, base_url, token, endpoint, params=None):
        try:
            response = client.get(
                base_url.rstrip("/") + endpoint,
                params=params,
                headers={
                    "token": token,
                    "x-requested-with": "XMLHttpRequest",
                    "cookie": f"satoken={token}",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("status")) != "200" or not isinstance(
                payload.get("data"), dict
            ):
                raise ValueError("DTB request was rejected")
            return payload["data"]
        except (httpx.HTTPError, TypeError, AttributeError) as exc:
            raise ValueError("DTB request failed") from exc

    def _pages(self, client, base_url, token, endpoint, key, identity, params, limit):
        rows, seen = [], set()
        page, expected = 1, None
        while True:
            data = self._get(
                client,
                base_url,
                token,
                endpoint,
                {**params, "page": page, "limit": limit},
            )
            try:
                batch = data[key]
                total = data.get("totalElements", data.get("total"))
                if not isinstance(batch, list) or isinstance(total, bool):
                    raise ValueError("Invalid DTB pagination")
                total = int(total)
                if total < 0 or (expected is not None and total != expected):
                    raise ValueError("Inconsistent DTB pagination")
                expected = total
                for row in batch:
                    if not isinstance(row, dict) or not isinstance(
                        row.get(identity), (str, int)
                    ):
                        raise ValueError("Invalid DTB row")
                    value = str(row[identity])
                    if value in seen:
                        raise ValueError("Duplicate DTB row")
                    seen.add(value)
                rows.extend(batch)
                if len(rows) == total:
                    return rows
                if not batch or len(rows) > total:
                    raise ValueError("Incomplete DTB pagination")
                page += 1
            except (KeyError, TypeError) as exc:
                raise ValueError("Invalid DTB pagination") from exc

    def list_tables(self, client, base_url, token, layer):
        return self._pages(
            client,
            base_url,
            token,
            "/api/data-map/table",
            "tableList",
            "tableId",
            {"layer": layer},
            500,
        )

    def table_detail(self, client, base_url, token, table_id):
        endpoint = "/api/data-map/detail/" + quote(str(table_id), safe="")
        data = self._get(client, base_url, token, endpoint)
        detail = data.get("table", data)
        if not isinstance(detail, dict):
            raise ValueError("Invalid DTB table detail")
        columns = self._pages(
            client,
            base_url,
            token,
            endpoint + "/column",
            "columnList",
            "columnName",
            {},
            200,
        )
        return detail, columns
