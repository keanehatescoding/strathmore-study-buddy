"""Admin chores: python -m app.admin_cli {claim-unowned,revoke-sessions,lockdown,delete-user} EMAIL

claim-unowned: courses synced before sign-in existed have no owner and are
hidden from everyone. They are claimed automatically when MOODLE_TOKEN_OWNER
signs in; this assigns them by hand.

revoke-sessions: signs a user out everywhere, e.g. after a lost device.
(Removing an address from ALLOWED_EMAILS already ends its sessions.)

lockdown: incident response for a leaked SECRET_KEY or database (see
docs/incident-response.md). Signs *every* user out, revokes each stored
Google grant with Google and forgets every stored Google/Moodle token.
Run it before rotating SECRET_KEY, or the stored tokens no longer decrypt
and their grants have to be killed by deleting the OAuth client instead.
Moodle tokens can't be revoked from here, so lockdown exits 1 while any remain
live (stored, forgotten, or MOODLE_TOKEN) until you delete them in Moodle and
pass --moodle-revoked.

delete-user: what Settings -> Delete account does, for a user who asks you
to: revokes their Google grant and deletes them with all their data. Take
the address off ALLOWED_EMAILS as well, or signing in starts a new account.
"""

from __future__ import annotations

import argparse

from sqlmodel import Session

from app.account import SyncRunning, delete_account
from app.auth import claim_unowned, find_user, lock_down, revoke_sessions
from app.db import engine


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    claim = sub.add_parser("claim-unowned", help="assign all unowned courses to a user")
    claim.add_argument("email")
    revoke = sub.add_parser("revoke-sessions", help="sign a user out on every device")
    revoke.add_argument("email")
    lock = sub.add_parser(
        "lockdown",
        help="sign everyone out and forget every stored Google/Moodle token "
        "(incident response; run before rotating SECRET_KEY)",
    )
    lock.add_argument(
        "--dry-run", action="store_true", help="report what would change, without changing it"
    )
    lock.add_argument(
        "--no-google-revoke",
        action="store_true",
        help="clear stored Google tokens without asking Google to revoke",
    )
    lock.add_argument(
        "--keep-moodle", action="store_true", help="leave stored Moodle tokens in place"
    )
    lock.add_argument(
        "--moodle-revoked",
        action="store_true",
        help="confirm every Moodle token (stored and MOODLE_TOKEN) was "
        "deleted in Moodle; without it, any Moodle token left live "
        "makes lockdown exit 1",
    )
    remove = sub.add_parser("delete-user", help="delete a user and all their data")
    remove.add_argument("email")
    args = parser.parse_args()

    with Session(engine) as session:
        if args.command == "lockdown":
            report = lock_down(
                session,
                revoke_google=not args.no_google_revoke,
                keep_moodle=args.keep_moodle,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                print(
                    f"dry run: would sign out every user and forget credentials "
                    f"for {report.cleared} of them"
                )
            else:
                print(f"signed out every user; forgot credentials for {report.cleared}")
                print(
                    f"google grants revoked: {report.google_revoked}"
                    + (
                        f", could not revoke: {report.google_failed}"
                        if report.google_failed
                        else ""
                    )
                    + (f", left alone: {report.google_skipped}" if report.google_skipped else "")
                )
            if report.undecryptable:
                print(
                    f"{report.undecryptable} stored Google token(s) did not decrypt "
                    "(SECRET_KEY already rotated?); delete the OAuth client at Google "
                    "to kill the grants behind them"
                )
            if report.google_skipped:
                print(
                    f"{report.google_skipped} stored Google token(s) were not revoked "
                    "(--no-google-revoke); revoke them or delete the OAuth client at "
                    "Google"
                )
            if report.shared_google:
                # --no-google-revoke wins over --dry-run: no revocation is attempted
                if args.no_google_revoke:
                    print(
                        "GOOGLE_REFRESH_TOKEN is configured as a shared fallback and was "
                        "not revoked (--no-google-revoke); revoke it or delete the "
                        "OAuth client, then replace or remove it"
                    )
                elif args.dry_run:
                    print(
                        "GOOGLE_REFRESH_TOKEN is configured as a shared fallback; dry "
                        "run: would revoke it with Google, then replace or remove it"
                    )
                elif report.shared_google_revoked:
                    print(
                        "revoked the shared GOOGLE_REFRESH_TOKEN with Google; replace or "
                        "remove it on every service"
                    )
                else:
                    print(
                        "could not revoke the shared GOOGLE_REFRESH_TOKEN; delete the "
                        "OAuth client at Google and replace or remove the variable"
                    )
            if report.shared_moodle:
                print(
                    "MOODLE_TOKEN is configured as a shared fallback; it is not revoked "
                    "here — delete it in Moodle and replace or remove it"
                )
            if report.moodle_cleared and not args.dry_run:
                print(
                    f"{report.moodle_cleared} Moodle token(s) were forgotten here but are "
                    "still valid at Moodle; delete them in Moodle: Site administration > "
                    "Server > Web services > Manage tokens"
                )
            if report.moodle_kept:
                print(
                    f"{report.moodle_kept} stored Moodle token(s) were left in place "
                    "(--keep-moodle); they stay live until deleted in Moodle"
                )
            # automation must not read a half-done lockdown as success: a grant
            # Google was not asked about, could not revoke, or we could not even
            # read the token for is still live until the operator deals with it.
            # Moodle tokens can never be revoked from here, so any that remain
            # (stored, forgotten, or the shared MOODLE_TOKEN) need the operator's
            # explicit --moodle-revoked acknowledgement.
            moodle_outstanding = report.shared_moodle or report.moodle_cleared or report.moodle_kept
            incomplete = (
                report.google_failed
                or report.undecryptable
                or report.google_skipped
                or (report.shared_google and not report.shared_google_revoked)
                or (moodle_outstanding and not args.moodle_revoked)
            )
            if incomplete and not args.dry_run and moodle_outstanding and not args.moodle_revoked:
                print(
                    "lockdown is NOT complete: Moodle tokens are still live. Delete "
                    "them in Moodle, then re-run with --moodle-revoked to confirm"
                )
            if incomplete and not args.dry_run:
                raise SystemExit(1)
            return
        user = find_user(session, args.email)
        if user is None:
            raise SystemExit(f"no such user {args.email} — log in via the web UI first")
        if args.command == "revoke-sessions":
            revoke_sessions(session, user)
            print(f"signed {user.email} out of every session")
            return
        if args.command == "delete-user":
            email = user.email
            try:
                revoked = delete_account(session, user)
            except SyncRunning:
                raise SystemExit(
                    f"a sync for {email} is running; nothing deleted, try again in a few minutes"
                ) from None
            print(f"deleted {email} and all their data")
            if not revoked:
                print(
                    "could not reach Google to revoke their grant; they can remove "
                    "it at myaccount.google.com/permissions"
                )
            return
        claimed, skipped = claim_unowned(session, user)
        print(f"assigned {claimed} unowned course(s) to {user.email}")
        if skipped:
            print(f"skipped {skipped} they already have their own copy of")


if __name__ == "__main__":
    main()
