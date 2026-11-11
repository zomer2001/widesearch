from searchos.local.library import LocalLibraryService


def test_chinese_bracket_aliases_bind_only_to_observed_sources():
    first = "local://library/document-one?rev=one#window"
    second = "local://library/document-two?rev=two#window"
    answer = "50亿美元【资料1】，15亿美元【资料2】；未知【资料3】。"
    bound = LocalLibraryService._bind_numbered_citations(answer, [first, second])
    assert f"[资料1]({first})" in bound
    assert f"[资料2]({second})" in bound
    assert "资料3（来源不可用）" in bound
    assert bound.count("local://") == 2


def test_existing_links_and_non_citation_brackets_are_preserved():
    ref = "local://library/document?rev=one#window"
    answer = f"[资料1]({ref})【注：金额含税】[source 1]"
    bound = LocalLibraryService._bind_numbered_citations(answer, [ref])
    assert bound == f"[资料1]({ref})【注：金额含税】[source 1]({ref})"
