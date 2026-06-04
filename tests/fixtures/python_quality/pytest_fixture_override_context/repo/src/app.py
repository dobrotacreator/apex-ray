class App:
    def __init__(self) -> None:
        self.dependency_overrides = {}


app = App()


def require_account() -> str:
    return "account-a"
