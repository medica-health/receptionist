import jwt

from receptionist.clients.tokens import LocalJwtIssuer

SECRET = "s" * 32


def test_patient_token_claims():
    issuer = LocalJwtIssuer(SECRET, "cdac-auth", "cdac-api")
    token = issuer.patient_token("u1", "a@b.c", "Asha")
    claims = jwt.decode(
        token, SECRET, algorithms=["HS256"], audience="cdac-api", issuer="cdac-auth"
    )
    assert claims["role"] == "PATIENT"
    assert claims["sub"] == "u1"
    assert claims["email"] == "a@b.c"
    assert claims["name"] == "Asha"
    assert claims["exp"] - claims["iat"] == 300
