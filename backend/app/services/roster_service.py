"""Exact username matching for an optional plain-text sampling roster."""
import re
from dataclasses import replace

from app.config import get_settings
from app.schemas.api import RosterPreview
from app.services.import_service import ImportBundle


def select_roster(bundle: ImportBundle, text: str | None) -> tuple[ImportBundle, RosterPreview | None]:
    if text is None:
        return bundle, None
    if len(text.encode("utf-8")) > 65536:
        raise ValueError("名单文件不能超过 64 KB")
    text = text.removeprefix("\ufeff")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        raise ValueError("请上传纯文本名单，每行填写一个学号（用户名）")
    tokens = [token for token in re.split(r"[\s,，;；]+", text.strip()) if token]
    if not tokens:
        raise ValueError("名单为空，请至少填写一个学号（用户名）")
    pattern = re.compile(get_settings().student_username_regex)
    invalid = [token for token in tokens if not pattern.fullmatch(token)]
    if invalid:
        raise ValueError("名单中含有不符合学号格式的内容，请仅保留学号（用户名），不要添加姓名、标题或序号")
    names = list(dict.fromkeys(tokens))
    students = set(bundle.students)
    eligible = {name for name, _ in bundle.selected}
    matched = [name for name in names if name in students and name in eligible]
    chosen = set(matched)
    selected = {key: value for key, value in bundle.selected.items() if key[0] in chosen}
    report = RosterPreview(requested_count=len(names), duplicate_count=len(tokens) - len(names),
        matched_students=matched, unknown_students=[name for name in names if name not in students],
        unavailable_students=[name for name in names if name in students and name not in eligible],
        selected_submission_snapshots=len(selected))
    return replace(bundle, students=matched, selected=selected,
        issues=[issue for issue in bundle.issues if issue.student_number in chosen]), report
