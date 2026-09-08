from __future__ import annotations

import io
import zipfile

import httpx

from app.config import Settings
from app.integrations.uoj.content_parser import UOJSubmissionContentParser
from app.integrations.uoj.schemas import UOJProblem, UOJSubmission


class UOJArchiveError(ValueError):
    pass


class UOJSubmissionArchiveClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.parser = UOJSubmissionContentParser()
        self._client = client

    async def read_source(self, submission: UOJSubmission, problem: UOJProblem) -> str:
        parsed = self.parser.parse(submission.content)
        url = (
            f"{self.settings.uoj_http_base_url.rstrip('/')}"
            f"/judge/download/submission/{parsed.bucket}/{parsed.random_id}"
        )
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        try:
            async with client.stream(
                "POST",
                url,
                data={
                    "judger_name": self.settings.uoj_judger_name,
                    "password": self.settings.uoj_judger_password,
                },
            ) as response:
                if response.status_code != 200:
                    raise UOJArchiveError(f"archive download returned HTTP {response.status_code}")
                chunks: list[bytes] = []
                length = 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > self.settings.archive_max_bytes:
                        raise UOJArchiveError("archive exceeds configured size limit")
                    chunks.append(chunk)
                archive = b"".join(chunks)
        finally:
            if own_client:
                await client.aclose()
        return self._extract_source(archive, problem)

    def _extract_source(self, archive: bytes, problem: UOJProblem) -> str:
        try:
            zip_file = zipfile.ZipFile(io.BytesIO(archive))
        except zipfile.BadZipFile as exc:
            raise UOJArchiveError("submission archive is not a valid ZIP") from exc

        with zip_file:
            infos = zip_file.infolist()
            if any(info.flag_bits & 0x1 for info in infos):
                raise UOJArchiveError("encrypted archives are unsupported")
            if len({info.filename for info in infos}) != len(infos):
                raise UOJArchiveError("archive contains duplicate file names")
            total = sum(info.file_size for info in infos)
            if total > self.settings.archive_uncompressed_max_bytes:
                raise UOJArchiveError("uncompressed archive exceeds configured size limit")
            if any(
                name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")
                for name in (info.filename for info in infos)
            ):
                raise UOJArchiveError("archive contains an unsafe path")

            source_names = [
                item.file_name
                for item in problem.submission_requirements
                if item.type == "source code"
            ]
            if not source_names:
                raise UOJArchiveError("problem does not declare a source-code file")

            parts: list[str] = []
            for source_name in source_names:
                try:
                    raw = zip_file.read(source_name)
                except KeyError as exc:
                    raise UOJArchiveError(f"archive is missing {source_name}") from exc
                text = self._decode(raw)
                if len(text) > self.settings.source_code_max_chars:
                    raise UOJArchiveError(f"{source_name} exceeds source-code length limit")
                if len(source_names) > 1:
                    parts.append(f"// FILE: {source_name}\n{text}")
                else:
                    parts.append(text)

            combined = "\n\n".join(parts)
            if len(combined) > self.settings.source_code_max_chars:
                raise UOJArchiveError("combined source exceeds source-code length limit")
            return combined

    @staticmethod
    def _decode(raw: bytes) -> str:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                pass
        raise UOJArchiveError("source file is not valid UTF-8 or GB18030 text")
