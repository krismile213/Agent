"""评测夹具文件: 用于 read/grep/统计类用例的确定性内容."""
import sys


def main():
    total = sum(i for i in range(1, 101))
    print(f"sum(1..100) = {total}")


if __name__ == "__main__":
    sys.exit(main())
