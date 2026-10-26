"""Data-only existing-file edits; patch notation is generated, never guessed."""
from collections import OrderedDict
from typing import Union
from typing_extensions import NotRequired, TypedDict


class TextEdit(TypedDict):
    path: str
    old: list[str]
    new: list[str]


TextEdit.__pydantic_config__ = {'extra': 'forbid'}


class PreparedEdit(TypedDict):
    edit_id: str
    start_line: NotRequired[int]
    end_line: NotRequired[int]
    new: NotRequired[list[str]]


PreparedEdit.__pydantic_config__ = {'extra': 'forbid'}
FileEdit = Union[TextEdit, PreparedEdit]


def prepared_edits(edits):
    return isinstance(edits, list) and any(isinstance(e, dict) and 'edit_id' in e for e in edits)


def validate_prepared_edits(edits):
    import re
    if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
        raise ValueError('Use 1..100 prepared edits')
    for edit in edits:
        if not isinstance(edit, dict) or not re.fullmatch('[a-f0-9]{32}', str(edit.get('edit_id', ''))):
            raise ValueError('Prepared edits require the edit_id returned by read_file(for_edit=true)')
        if set(edit) == {'edit_id'}:
            continue
        if set(edit) not in ({'edit_id', 'new'}, {'edit_id', 'start_line', 'end_line', 'new'}):
            raise ValueError('Use edit_id/new for the whole file, or include both start_line/end_line')
        if 'start_line' in edit:
            start, end = edit['start_line'], edit['end_line']
            if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                raise ValueError('Prepared line ranges are 1-based and inclusive')
        if not isinstance(edit['new'], list) or any(not isinstance(v, str) or any(c in v for c in '\r\n\x00') for v in edit['new']):
            raise ValueError('new must contain individual text lines')


def edits_patch(edits):
    if not isinstance(edits, list) or not edits:
        raise ValueError('edits must be a nonempty array of {path, old, new}')
    grouped = OrderedDict()
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) != {'path', 'old', 'new'}:
            raise ValueError(f'edits[{index}] requires exactly path, old and new')
        path = edit['path']
        if not isinstance(path, str) or not path or any(ord(c) < 32 for c in path):
            raise ValueError(f'edits[{index}].path must be a nonempty path without control characters')
        for key in ('old', 'new'):
            lines = edit[key]
            if not isinstance(lines, list) or (key == 'old' and not lines) or any(
                    not isinstance(line, str) or any(c in line for c in ('\r', '\n', '\x00')) for line in lines):
                raise ValueError(f'edits[{index}].{key} must contain individual text lines; old cannot be empty')
        grouped.setdefault(path, []).append(edit)
    patch = ['*** Begin Patch']
    for path, changes in grouped.items():
        patch.append('*** Update File: ' + path)
        for edit in changes:
            patch.append('@@')
            patch.extend('-' + line for line in edit['old'])
            patch.extend('+' + line for line in edit['new'])
    patch.append('*** End Patch')
    return '\n'.join(patch) + '\n'
