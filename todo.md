1. agents.py 的代码需要从 threading.Thread 变成 subprocess 的形式， 这样才能实现用户可以任意终止 LLM 过程， Python 中无法终止正在运行的 thread。 所有 agents.py 需要调用 Elisp 的地方， 都要从 subprocess 先和 emigo python 通讯再中转。
2. 是否要对 AI 输出的代码也做语法高亮？ 我感觉不需要吧， diff 高亮就可以了， 如果没有输出 diff, 基本上也没啥用
3. 怎么根据 AI 输出生成 diff files 列表？ Aidermacs 代码搬运过来？ 每个项目都要按照文件粒度缓存补丁
4. diff review 的界面： 左边铺满， 左边上面分别是 "全部文件、文件 A、文件 B"， 左边下面是 "全部文件的 hunks, 文件 A 的 hunks, 文件 B 的 hunks", 支持整个文件 apply/cancel 和 hunk 的 apply/cancel
5. 右侧栏应该显示所有 session 的状态，方便用户知道 AI 干完活以后，手动切换 session
6. 研究 Cursor 的提示词， 看看能否用 RAG 的方式来增强 aider tree-sitter 这种 repomap 的方式？ 我总感觉 Cursor 的那种模式要高级一点， aider 适合自己的项目精确重构， Cursor 适应范围要广很多
7. 可以随时更改过去的某个 prompt，然后重新发给 LLM, 执行这个命令的时候， 建议临时取消 read-only 后， 编辑后重新发送
