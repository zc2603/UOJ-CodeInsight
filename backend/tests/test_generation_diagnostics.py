from app.generation_diagnostics import safe_error


def test_error_diagnostics_only_allow_known_metadata():
    assert safe_error(None) is None
    assert safe_error('出题未完成（TimeoutError）') == 'TimeoutError'
    assert safe_error('出题未完成（LLMProviderError）') == 'LLMProviderError'
    assert safe_error('Worker lease expired') == 'Worker lease expired'
    assert safe_error('secret source or credential') == 'Other error (details withheld)'
