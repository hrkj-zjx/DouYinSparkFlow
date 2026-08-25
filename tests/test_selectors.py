import unittest

import core.tasks as tasks


class SelectorTests(unittest.TestCase):
    def test_conversation_item_selector_uses_class_selector_so_pinned_items_match(self):
        """锁定已线上观察到的会话确认与唯一 Slate 编辑器选择器。"""

        self.assertEqual(tasks.CONVERSATION_ITEM_SELECTOR, '.conversationConversationItemwrapper')
        self.assertEqual(tasks.CONVERSATION_TITLE_SELECTOR, '.conversationConversationItemtitle')
        self.assertEqual(tasks.CONVERSATION_LIST_SELECTOR, '.conversationConversationListwrapper')
        self.assertEqual(
            tasks.CONVERSATION_INDEX_ANCESTOR_SELECTOR,
            'xpath=ancestor::*[@data-index][1]',
        )
        self.assertEqual(
            tasks.CURRENT_CONVERSATION_CLASS,
            'conversationConversationItemcurConversation',
        )
        self.assertEqual(tasks.RIGHT_PANEL_TITLE_SELECTOR, '.RightPanelHeadertitle')
        self.assertEqual(
            tasks.CHAT_EDITOR_CONTAINER_SELECTOR,
            '.messageEditorimChatEditorContainer',
        )
        self.assertEqual(
            tasks.CHAT_EDITOR_SELECTOR,
            '.messageEditorimChatEditorContainer '
            '[data-slate-editor="true"][contenteditable="true"]',
        )
        self.assertEqual(
            tasks.SLATE_TEXT_LEAF_SELECTOR,
            '[data-slate-leaf="true"]',
        )
        self.assertEqual(
            tasks.SLATE_TEXT_STRING_SELECTOR,
            '[data-slate-string="true"]',
        )


if __name__ == '__main__':
    unittest.main()
