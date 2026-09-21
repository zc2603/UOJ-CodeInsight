"""The structural edition must retain every baseline policy character in order."""
import re
from pathlib import Path
import pytest


def policy_text(text):
    text = re.sub(r'^#{1,6} .*\n?', '', text, flags=re.M)
    text = re.sub(r'^\s*(?:- |\d+\. )', '', text, flags=re.M)
    return re.sub(r'\s+', '', text)


@pytest.mark.parametrize('old,new', [
    ('question_generator_v16','question_generator_v18'),
    ('grader_v8','grader_v10'),
    ('question_quality_v3','question_quality_v5'),
])
def test_structure_preserves_baseline_policy(old,new):
    root=Path(__file__).parents[1]/'app/prompts'
    # User explicitly authorized this one addition after the structural edition.
    new_text=(root/(new+'.txt')).read_text(encoding='utf-8-sig')
    if new=='question_generator_v18':
        assert new_text.count('- 题干可以使用 Markdown 格式。\n')==1
        new_text=new_text.replace('- 题干可以使用 Markdown 格式。\n','')
    assert policy_text((root/(old+'.txt')).read_text(encoding='utf-8-sig')) == policy_text(new_text)
