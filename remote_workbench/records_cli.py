"""Read saved local records without an App, HTTP listener, SSH or model call."""
import argparse
import json
from pathlib import Path
import re

from remote_workbench import audit
from remote_workbench.workspace_router import default_state_dir


def main(argv=None):
    parser = argparse.ArgumentParser(prog='remote-workbench records', description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=default_state_dir(), help='MCP local state directory')
    parser.add_argument('--workspace-id', required=True, help='Workspace ID from workspace_info')
    parser.add_argument('--id', help='Saved operation/output ID; omit to list recent records')
    parser.add_argument('--stream', choices=sorted(audit.STREAMS), help='Read a bounded page from the saved stream')
    parser.add_argument('--cursor', type=int, default=0)
    parser.add_argument('--max-bytes', type=int, default=8192)
    parser.add_argument('--limit', type=int, default=30)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-f0-9]{32}', args.workspace_id):
        parser.error('workspace-id must be a 32-character lowercase hex ID')
    if args.stream and not args.id:
        parser.error('--stream requires --id')
    directory = args.data_dir.expanduser().resolve() / 'audit' / args.workspace_id
    if not directory.is_dir():
        parser.error('workspace record directory does not exist')
    try:
        with audit.scoped_root(directory):
            if args.stream:
                result = audit.read_output(args.id, args.stream, args.cursor, args.max_bytes)
            elif args.id:
                result = json.loads((audit.directory(args.id) / 'record.json').read_text())
            else:
                result = {'records': audit.recent(args.limit)}
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
