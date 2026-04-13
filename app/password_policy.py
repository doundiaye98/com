MIN_PASSWORD_LENGTH = 8


def validate_password_for_user(pwd: str) -> str | None:
    """Retourne un message d'erreur ou None si le mot de passe est acceptable."""
    if len(pwd) < MIN_PASSWORD_LENGTH:
        return f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} caractères."
    if not any(c.isalpha() for c in pwd):
        return "Le mot de passe doit contenir au moins une lettre."
    if not any(c.isdigit() for c in pwd):
        return "Le mot de passe doit contenir au moins un chiffre."
    return None
