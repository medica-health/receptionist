from receptionist.identity.names import names_match, normalize_name


def test_normalize_strips_honorific_and_ji():
    assert normalize_name("Mr. Abhinav  Singh ji") == "abhinav singh"


def test_matches():
    assert names_match("abhinav singh", "Abhinav Singh")
    assert names_match("Mr. Abhinav  Singh", "Abhinav Singh")
    assert names_match("Singh Abhinav", "Abhinav Singh")
    assert names_match("Abhinav Sing", "Abhinav Singh")


def test_non_matches():
    assert not names_match("Ankit Singh", "Abhinav Singh")
    assert not names_match("Priya", "Priyanka Sharma")
