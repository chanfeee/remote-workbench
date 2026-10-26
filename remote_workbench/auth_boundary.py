"""A narrow authentication workflow guard, not a shell or credential sandbox."""
import os
import re
import shlex

from remote_workbench.call_arguments import command_text


def authentication_command(command):
    """Recognize common wrappers around an explicit ``sshpass`` invocation.

    This is a workflow guard for commands entered directly into Remote
    Workbench. It is intentionally not a shell sandbox: arbitrary scripts can
    still hide their behavior. Parse only enough shell syntax to find command
    words and a short list of common prefix utilities without mistaking a
    search or echo argument such as ``rg sshpass README`` for authentication.
    """
    if isinstance(command, dict):
        return any(authentication_command(argv) for argv in command.get('commands', []))
    if isinstance(command, list):
        command = command_text(command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return False

    punctuation = set(';&|()\n')
    segments = [[]]
    for word in words:
        if word and all(char in punctuation for char in word):
            segments.append([])
        else:
            segments[-1].append(word)

    def assignment(word):
        return re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', word) is not None

    def command_is_authentication(segment, index=0, depth=0):
        if depth > 8:
            return False
        while index < len(segment):
            word = segment[index]
            if assignment(word):
                if word.startswith('SSHPASS='):
                    return True
                index += 1
                continue

            executable = os.path.basename(word)
            if executable == 'sshpass':
                return True
            if executable == 'sudo':
                index += 1
                while index < len(segment):
                    option = segment[index]
                    if option == '--':
                        index += 1
                        break
                    if not option.startswith('-') or option == '-':
                        break
                    if option in {'-u', '--user', '-g', '--group', '-h', '--host',
                                  '-p', '--prompt', '-C', '--close-from', '-r', '--role',
                                  '-t', '--type'}:
                        index += 2
                    elif any(option.startswith(prefix) and option != prefix for prefix in
                             ('-u', '-g', '-h', '-p', '-C', '-r', '-t', '--user=',
                              '--group=', '--host=', '--prompt=', '--close-from=',
                              '--role=', '--type=')):
                        index += 1
                    else:
                        index += 1
                continue
            if executable == 'time':
                index += 1
                while index < len(segment) and segment[index].startswith('-'):
                    option = segment[index]
                    if option == '--':
                        index += 1
                        break
                    if option in {'-f', '--format', '-o', '--output'}:
                        index += 2
                    elif (option.startswith('--format=') or option.startswith('--output=')
                          or option.startswith('-f') and option != '-f'
                          or option.startswith('-o') and option != '-o'):
                        index += 1
                    elif option in {'-a', '--append', '-p', '--portability', '-q',
                                    '--quiet', '-v', '--verbose', '--help', '--version'}:
                        index += 1
                    else:
                        break
                continue
            if executable == 'timeout':
                index += 1
                while index < len(segment) and segment[index].startswith('-'):
                    option = segment[index]
                    if option == '--':
                        index += 1
                        break
                    if option in {'-k', '--kill-after', '-s', '--signal'}:
                        index += 2
                    elif (option.startswith('--kill-after=') or option.startswith('--signal=')):
                        index += 1
                    elif option in {'-f', '--foreground', '--preserve-status', '-v', '--verbose'}:
                        index += 1
                    else:
                        break
                if index < len(segment):  # timeout's duration argument
                    index += 1
                continue
            if executable == 'env':
                index += 1
                while index < len(segment):
                    option = segment[index]
                    if option == '--':
                        index += 1
                        break
                    if assignment(option):
                        if option.startswith('SSHPASS='):
                            return True
                        index += 1
                        continue
                    if option in {'-i', '--ignore-environment', '-0', '--null'}:
                        index += 1
                        continue
                    if option in {'-u', '--unset', '-C', '--chdir'}:
                        index += 2
                        continue
                    if option.startswith(('--unset=', '--chdir=')) or (
                        option.startswith('-u') and option != '-u'
                    ) or (option.startswith('-C') and option != '-C'):
                        index += 1
                        continue
                    if option in {'-S', '--split-string'}:
                        if index + 1 >= len(segment):
                            return False
                        try:
                            nested = shlex.split(segment[index + 1], posix=True)
                        except ValueError:
                            return False
                        return command_is_authentication(nested, depth=depth + 1)
                    if option.startswith('--split-string='):
                        try:
                            nested = shlex.split(option.split('=', 1)[1], posix=True)
                        except ValueError:
                            return False
                        return command_is_authentication(nested, depth=depth + 1)
                    break
                continue
            if executable == 'command':
                index += 1
                if index < len(segment) and segment[index] in {'-v', '-V'}:
                    return False
                if index < len(segment) and segment[index] == '-p':
                    index += 1
                continue
            if executable == 'exec':
                index += 1
                if index < len(segment) and segment[index] == '-a':
                    index += 2
                continue
            if executable == 'nohup':
                index += 1
                if index < len(segment) and segment[index] == '--':
                    index += 1
                continue
            return False
        return False

    for segment in segments:
        if command_is_authentication(segment):
            return True
    return False


def refusal():
    return {'error': 'authentication_requires_user_terminal', 'executed': False,
            'message': 'Establish SSH authentication in your own terminal, then reuse the existing ControlMaster. Authentication arguments are omitted from the operation log.'}
