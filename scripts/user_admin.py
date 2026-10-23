"""命令行账号管理：初始化管理员、改密、调整角色或停用账号。

用于首次部署，或在管理员忘记密码且没有其他管理员可用时恢复访问。
会按需执行数据库迁移；不会打印密码或密码摘要。
"""

import argparse
import asyncio
import getpass

from app.config import Settings
from app.database import Database
from app.errors import ChatError
from app.users import ROLES, AuthService


def parse_args():
    parser = argparse.ArgumentParser(description="管理 RAG 工作空间账号")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="创建账号；系统还没有账号时创建为首个管理员")
    add.add_argument("--username", required=True)
    add.add_argument("--display-name", default="")
    add.add_argument("--role", choices=ROLES, default="viewer")
    add.add_argument("--password", help="省略时交互输入")

    passwd = sub.add_parser("passwd", help="重置密码并失效该账号全部会话")
    passwd.add_argument("--username", required=True)
    passwd.add_argument("--password", help="省略时交互输入")

    role = sub.add_parser("role", help="调整角色")
    role.add_argument("--username", required=True)
    role.add_argument("--role", required=True, choices=ROLES)

    for name, help_text in (("disable", "停用账号"), ("enable", "启用账号")):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--username", required=True)

    sub.add_parser("list", help="列出账号")
    return parser.parse_args()


def prompt_password(value: str | None) -> str:
    if value:
        return value
    first = getpass.getpass("新密码: ")
    second = getpass.getpass("再次输入: ")
    if first != second:
        raise SystemExit("两次输入的密码不一致。")
    return first


async def run(args):
    settings = Settings()
    database = Database(settings)
    async with database.pool:
        await database.setup()
        auth = AuthService(settings)
        auth.bind(database)

        if args.command == "list":
            for user in await auth.list_users():
                status = "启用" if user.is_active else "停用"
                print(f"{user.username:<20} {user.display_name:<16} {user.role:<7} {status}")
            return

        user = await auth.find(args.username)
        if args.command == "add":
            password = prompt_password(args.password)
            if not await auth.has_users():
                _, _, created = await auth.setup(
                    username=args.username,
                    password=password,
                    display_name=args.display_name,
                )
                print(f"已初始化管理员：{created.username}")
                return
            created = await auth.create_user(
                username=args.username,
                password=password,
                role=args.role,
                display_name=args.display_name,
            )
            print(f"已创建账号：{created.username}（{created.role}）")
        elif user is None:
            raise SystemExit(f"用户不存在：{args.username}")
        elif args.command == "passwd":
            await auth.update_user(user.user_id, password=prompt_password(args.password))
            print(f"已重置 {user.username} 的密码，旧会话已失效。")
        elif args.command == "role":
            await auth.update_user(user.user_id, role=args.role)
            print(f"已将 {user.username} 的角色改为 {args.role}。")
        elif args.command in ("disable", "enable"):
            await auth.update_user(user.user_id, is_active=args.command == "enable")
            print(f"已{'启用' if args.command == 'enable' else '停用'} {user.username}。")


def main():
    args = parse_args()
    try:
        asyncio.run(run(args))
    except ChatError as error:
        raise SystemExit(f"{error.code}: {error}") from None


if __name__ == "__main__":
    main()
