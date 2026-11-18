"""Keep package imports free of API-key and application initialization side effects."""


def main():
    from abscribe_backend.app import main as run
    return run()
