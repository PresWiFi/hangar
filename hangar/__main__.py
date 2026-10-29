"""Run Hangar, or a maintenance command.

    python -m hangar                    start the server
    python -m hangar reset-password     give the owner a new random password (signs every device out)
                                        and turn password sign-in back on
"""
import argparse
import secrets
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m hangar")
    sub = parser.add_subparsers(dest="command")
    reset = sub.add_parser("reset-password", help="set a new random owner password and print it")
    reset.add_argument("--username", help="also rename the owner account")
    args = parser.parse_args()

    if args.command == "reset-password":
        reset_password(args.username)
        return

    import uvicorn

    from .config import settings
    uvicorn.run("hangar.app:app", host=settings.host, port=settings.port, proxy_headers=True,
                forwarded_allow_ips="*", ws_ping_interval=20, log_level="warning")


def reset_password(username: str | None) -> None:
    from . import auth
    if auth.setup_needed():
        sys.exit("Hangar isn't set up yet. Open it in a browser and use the setup code from its log.")
    if username:
        try:
            auth.set_username(auth.check_username(username))
        except ValueError as e:
            sys.exit(str(e))
    password = secrets.token_urlsafe(12)
    auth.set_password(password)
    from . import oidc
    if not oidc.password_login_enabled():
        oidc.set_password_login(True)
        print("Password sign-in was turned off; it's on again.")
    print(f"Username: {auth.owner()['username']}")
    print(f"Password: {password}")
    print("Every device is signed out. Change this password in Settings after you sign in.")


if __name__ == "__main__":
    main()
