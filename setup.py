"""Interactive, dependency-free one-time setup. Do not commit data/config.json."""
import getpass
import json
from pathlib import Path
import secrets
from app import CONFIG_PATH, DB_PATH, password_hash, init_db


def prompt_password(label, minimum):
    while True:
        password = getpass.getpass(f'{label} ({minimum}+ characters): ')
        confirm = getpass.getpass('Repeat password: ')
        if len(password) < minimum:
            print('Password is too short.')
        elif password != confirm:
            print('Passwords did not match.')
        else:
            return password


def entry(password):
    salt = secrets.token_hex(16)
    return {'salt': salt, 'hash': password_hash(password, salt)}


def main():
    print('\n=== EventDesk initial setup ===')
    if CONFIG_PATH.exists():
        print('Configuration exists at', CONFIG_PATH)
        print('Nothing changed. To reset passwords, make a secure backup and configure new passwords.')
        return
    name = input('Event name [Company Annual Party]: ').strip() or 'Company Annual Party'
    admin = prompt_password('Admin password', 10)
    staff = prompt_password('Staff/station password', 8)
    if admin == staff:
        print('Admin and staff passwords must differ.')
        return
    config = {
        'event_name': name[:90],
        'secret': secrets.token_hex(32),
        'admin_password': entry(admin),
        'staff_password': entry(staff)
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'x', encoding='utf-8') as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    init_db()
    print('\nSetup complete.')
    print('Start the server with: python app.py')
    print('Your database is:', DB_PATH)
    print('Back up the data folder. Never share config.json publicly.')


if __name__ == '__main__':
    main()
