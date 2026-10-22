import warnings
warnings.filterwarnings(
    "ignore",
    message=r"pkg_resources is deprecated as an API.*",
    category=UserWarning,
)

from app import ATI

def main():
    ATI.main()

if __name__ == "__main__":
    main()