from app import markdown_to_slack_mrkdwn


class TestBulletList:
    def test_hyphen_bullet(self):
        assert markdown_to_slack_mrkdwn("- item1\n- item2") == "• item1\n• item2"

    def test_asterisk_bullet(self):
        assert markdown_to_slack_mrkdwn("* item1\n* item2") == "• item1\n• item2"

    def test_nested_bullet(self):
        assert markdown_to_slack_mrkdwn("- parent\n  - child") == "• parent\n  • child"

    def test_bullet_not_in_code_block(self):
        text = "```\n- not a bullet\n```"
        assert markdown_to_slack_mrkdwn(text) == text


class TestHeading:
    def test_h1(self):
        assert markdown_to_slack_mrkdwn("# Title") == "*Title*"

    def test_h2(self):
        assert markdown_to_slack_mrkdwn("## Section") == "*Section*"

    def test_h3(self):
        assert markdown_to_slack_mrkdwn("### Subsection") == "*Subsection*"


class TestLink:
    def test_link(self):
        assert (
            markdown_to_slack_mrkdwn("[click](https://example.com)")
            == "<https://example.com|click>"
        )


class TestBold:
    def test_bold(self):
        assert markdown_to_slack_mrkdwn("**bold text**") == "*bold text*"

    def test_bold_in_sentence(self):
        assert (
            markdown_to_slack_mrkdwn("this is **bold** word")
            == "this is *bold* word"
        )


class TestItalic:
    def test_italic(self):
        assert markdown_to_slack_mrkdwn("*italic text*") == "_italic text_"


class TestStrikethrough:
    def test_strikethrough(self):
        assert markdown_to_slack_mrkdwn("~~deleted~~") == "~deleted~"


class TestInlineCode:
    def test_inline_code_preserved(self):
        assert markdown_to_slack_mrkdwn("`code`") == "`code`"

    def test_bold_inside_inline_code_not_converted(self):
        assert markdown_to_slack_mrkdwn("`**not bold**`") == "`**not bold**`"


class TestCodeBlock:
    def test_code_block_preserved(self):
        text = "```python\ndef hello():\n    pass\n```"
        assert markdown_to_slack_mrkdwn(text) == text

    def test_markdown_inside_code_block_not_converted(self):
        text = "```\n## not a heading\n**not bold**\n```"
        assert markdown_to_slack_mrkdwn(text) == text


class TestTable:
    def test_table_to_code_block(self):
        md = "| Name | Age |\n|------|-----|\n| Alice | 30 |"
        result = markdown_to_slack_mrkdwn(md)
        assert result.startswith("```")
        assert result.endswith("```")
        assert "| Name | Age |" in result
        assert "| Alice | 30 |" in result
        # セパレーター行は除去される
        assert "---" not in result


class TestCombined:
    def test_mixed_content(self):
        md = "## Title\n\n- **item1**\n- item2\n\nsome `code` here"
        result = markdown_to_slack_mrkdwn(md)
        assert result.startswith("*Title*")
        assert "• *item1*" in result
        assert "• item2" in result
        assert "`code`" in result
