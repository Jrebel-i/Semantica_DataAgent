from typing import ClassVar


class Adapter:
    id = "third_party"
    version = "1.0.0"
    display_name = "Third party"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {"prefix": {"type": "string", "minLength": 2}},
        "required": ["prefix"],
        "additionalProperties": False,
    }

    def read(self, directory, parameters):
        import hashlib

        from semantica_dataagent.canonical import ReadResult, Record

        source = "third_party.source"
        digest = hashlib.sha256((directory / source).read_bytes()).hexdigest()
        return ReadResult(
            [
                Record(
                    "table",
                    {"database": "prod", "name": parameters["prefix"], "comment": ""},
                    source,
                )
            ],
            {source: digest},
        )
