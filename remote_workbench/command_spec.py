"""Structured commands; batches contain sequential argv steps, never shell code."""
import json
from typing import Union
from typing_extensions import NotRequired, TypedDict


class CommandBatch(TypedDict):
    commands: list[list[str]]
    stop_on_error: NotRequired[bool]


CommandBatch.__pydantic_config__ = {'extra': 'forbid'}
Command = Union[str, list[str], CommandBatch]


def validate_batch(value):
    if not isinstance(value, dict) or not {'commands'} <= set(value) <= {'commands', 'stop_on_error'}:
        raise ValueError('Batch requires commands and optional stop_on_error')
    commands = value['commands']
    if not isinstance(commands, list) or not 1 <= len(commands) <= 32:
        raise ValueError('Batch requires 1..32 sequential argv commands')
    if 'stop_on_error' in value and type(value['stop_on_error']) is not bool:
        raise ValueError('stop_on_error must be boolean')
    for argv in commands:
        if (not isinstance(argv, list) or not argv or not all(isinstance(s, str) and '\x00' not in s for s in argv)
                or not argv[0]):
            raise ValueError('Each batch command must be a nonempty argv array without NUL')
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 1048576:
        raise ValueError('Batch input exceeds 1 MiB')


def normalize_arguments(name, arguments):
    """Only known, unambiguous aliases; conflicts and unknown fields still fail."""
    if arguments is not None and not isinstance(arguments, dict):
        raise ValueError('arguments must be an object')
    arguments = dict(arguments or {})
    if name == 'workspace_tools' and isinstance(arguments.get('arguments'), dict):
        arguments['arguments'] = normalize_arguments(arguments.get('tool'), arguments['arguments'])
    if name in {'exec_command', 'ssh_run', 'workspace_switch', 'apply_patch'}:
        for alias, canonical in [('argv', 'command'), ('cmd', 'command'), ('workdir', 'cwd')]:
            if alias not in arguments:
                continue
            if canonical in arguments:
                raise ValueError(f'Pass {canonical} or {alias}, not both')
            value = arguments.pop(alias)
            if alias == 'argv' and not isinstance(value, list):
                raise ValueError('argv must be an argument array; shell text uses command')
            arguments[canonical] = value
        if isinstance(arguments.get('command'), dict):
            # Reject invalid data before Pydantic can coerce booleans/arrays.
            validate_batch(arguments['command'])
    return arguments
