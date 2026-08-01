from apex_ray.path_matching import path_matches_any


def test_exact_path_with_glob_metacharacters_matches_literally() -> None:
    path = "src/pages/[id].ts"

    assert path_matches_any(path, [path]) is True
