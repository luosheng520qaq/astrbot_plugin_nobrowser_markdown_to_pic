import asyncio
import os
import tempfile

import pillowmd


async def main():
    img = await pillowmd.MdToImage("# 测试\n- 列表项\n`code`")
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    if hasattr(img, "image"):
        img.image.save(path)
    else:
        img.save(path)
    print(path)


if __name__ == "__main__":
    asyncio.run(main())