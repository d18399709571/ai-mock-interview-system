"""Interactively create the first administrator account."""
import asyncio
from getpass import getpass

from sqlalchemy import select

from app.db.session import async_session
from app.models.admin import Admin


async def seed() -> None:
    email = input("管理员邮箱: ").strip().lower()
    if not email or "@" not in email:
        raise SystemExit("邮箱格式无效。")

    async with async_session() as db:
        result = await db.execute(select(Admin).where(Admin.email == email))
        if result.scalar_one_or_none():
            print("该管理员已存在，跳过创建。")
            return

        password = getpass("管理员密码（至少 12 个字符）: ")
        confirmation = getpass("再次输入密码: ")
        if len(password) < 12:
            raise SystemExit("密码至少需要 12 个字符。")
        if len(password.encode("utf-8")) > 72:
            raise SystemExit("密码不能超过 72 个 UTF-8 字节。")
        if password != confirmation:
            raise SystemExit("两次输入的密码不一致。")

        db.add(Admin(
            email=email,
            first_name="Admin",
            last_name="Prepwise",
            password=Admin.get_password_hash(password),
            role="superadmin",
            is_active=True,
        ))
        await db.commit()
        print(f"超级管理员已创建: {email}")


if __name__ == "__main__":
    asyncio.run(seed())
