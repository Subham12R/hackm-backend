from secrets import choice

# No 0/O/1/I/L so codes read cleanly aloud; unrelated to the registration id.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def new_anonymous_code() -> str:
    return "HM-" + "".join(choice(ALPHABET) for _ in range(5))
