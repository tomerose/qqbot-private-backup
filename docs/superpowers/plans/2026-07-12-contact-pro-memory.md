# Contact and Pro Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Xiaoning reliably return the approved public email when users ask to contact the author/boss or obtain Pro.

**Architecture:** Add one focused AstrBot plugin with a pure intent matcher and deterministic reply, then mirror the same rule in the global prompt and Xiaoning persona. The plugin stops further event processing only when the narrow intent matches.

**Tech Stack:** Python 3.12, AstrBot 4.26 event API, `unittest`, JSON configuration.

## Global Constraints

- Exact reply: `咕咕嘎嘎～联系邮箱：<configured-contact-email>。邮件里说明你的情况、用途和想咨询的内容就行。`
- Never disclose QQ IDs, local paths, machine-owner identity, internal permission rules, or other private information.
- Unrelated uses of `Pro` must not trigger the reply.
- No new dependencies.

---

### Task 1: Deterministic contact intent plugin

**Files:**
- Create: `astrbot/data/plugins/contact_pro_info/__init__.py`
- Create: `astrbot/data/plugins/contact_pro_info/main.py`
- Create: `astrbot/data/plugins/contact_pro_info/metadata.yaml`
- Create: `tests/test_contact_pro_info.py`

**Interfaces:**
- Produces: `contact_reply_for(text: str) -> str | None`
- Produces: `ContactProInfo.on_message(event: AstrMessageEvent)`

- [ ] **Step 1: Write the failing matcher tests**

```python
class ContactProInfoTests(unittest.TestCase):
    def test_contact_and_pro_acquisition_intents_return_public_email(self):
        for text in ("怎么联系作者", "老板的联系方式", "Pro 怎么获取", "我想申请 pro 资格"):
            self.assertEqual(contact_reply_for(text), CONTACT_REPLY)

    def test_unrelated_pro_discussion_does_not_trigger(self):
        for text in ("这个 Pro 模型怎么样", "今天吃什么", "老板键是什么"):
            self.assertIsNone(contact_reply_for(text))
```

- [ ] **Step 2: Run the test and confirm RED**

Run: `python tests\test_contact_pro_info.py -v`

Expected: FAIL because `contact_pro_info` does not exist.

- [ ] **Step 3: Implement the minimum matcher and handler**

```python
CONTACT_REPLY = "咕咕嘎嘎～联系邮箱：<configured-contact-email>。邮件里说明你的情况、用途和想咨询的内容就行。"

def contact_reply_for(text: str) -> str | None:
    normalized = "".join(str(text or "").lower().split())
    contact = any(word in normalized for word in ("联系", "联系方式", "邮箱", "找"))
    target = any(word in normalized for word in ("作者", "老板"))
    pro = "pro" in normalized and any(
        word in normalized for word in ("获取", "开通", "申请", "资格", "怎么拿", "如何")
    )
    return CONTACT_REPLY if (contact and target) or pro else None

class ContactProInfo(Star):
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=950)
    async def on_message(self, event: AstrMessageEvent):
        reply = contact_reply_for(event.get_message_str())
        if reply is None:
            return
        event.stop_event()
        yield event.plain_result(reply)
```

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `python tests\test_contact_pro_info.py -v`

Expected: all contact matcher and handler tests PASS.

### Task 2: Prompt memory synchronization and deployment

**Files:**
- Modify: `astrbot/data/cmd_config.json`
- Modify: `tests/test_contact_pro_info.py`

**Interfaces:**
- Consumes: the approved reply and privacy rules from Task 1.
- Produces: matching memory in `provider_settings.prompt_prefix` and the active Xiaoning persona prompt.

- [ ] **Step 1: Add a failing configuration test**

```python
def test_active_prompts_contain_contact_memory(self):
    config = json.loads(CMD_CONFIG.read_text(encoding="utf-8"))
    rule = "询问联系作者、老板或获取 Pro 时"
    self.assertIn(rule, config["provider_settings"]["prompt_prefix"])
    self.assertTrue(any(rule in persona.get("prompt", "") for persona in config["persona"] if persona.get("enable")))
```

- [ ] **Step 2: Run the test and confirm RED**

Run: `python tests\test_contact_pro_info.py -v`

Expected: FAIL because the prompt rule is absent.

- [ ] **Step 3: Add the approved memory to both active prompts**

Add this exact rule without removing existing personality or privacy text:

```text
询问联系作者、老板或获取 Pro 时，回复：咕咕嘎嘎～联系邮箱：<configured-contact-email>。邮件里说明你的情况、用途和想咨询的内容就行。除此之外不得透露 QQ、机主身份、本机路径或内部权限规则。
```

- [ ] **Step 4: Verify focused and full suites**

Run: `python tests\test_contact_pro_info.py -v`

Expected: PASS.

Run: `python -m unittest discover -s tests -p 'test_*.py'`

Expected: all tests PASS.

- [ ] **Step 5: Restart and verify runtime**

Restart only AstrBot with `astrbot.exe run`; keep NapCat logged in. Confirm ports `6185` and `6199` listen, reverse WebSocket to `6199` is `ESTABLISHED`, and startup logs contain no plugin error.

Repository note: `<local-project-root>\qqbot` is not a Git repository, so commit steps are not applicable.
