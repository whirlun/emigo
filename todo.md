1.输入区域支持 @ 文件名补全
   - (English: and the input area should support @ filename completion.)
2. AI 输出后的语法高亮处理
   - (English: Syntax highlighting for AI output.)
3. AI 输出完成后， 根据 diff 的内容， 更新 “补丁文件列表”， 并在左边弹出类似 magit 的 diff apply/cancel 操作 ，现在 ediff 的分屏 review diff 太难用了
   - (English: After AI output is complete, update the "patch file list" based on the diff content, and pop up a magit-like diff apply/cancel operation on the left. The current ediff split-screen diff review is too difficult to use.)
4. 切换不同的工作区的时候， 侧边栏是否需要同步显示？ 这个我还没有想清楚， 自动切方便， 但是自动切侧边栏的内容是不是也不是用户想要的？
   - (English: When switching between different workspaces, does the sidebar need to be synchronized? I haven't figured this out yet. Automatic switching is convenient, but is the automatically switched sidebar content what the user wants?)
5. 研究 Cursor 的提示词， 看看能否用 RAG 的方式来增强 aider tree-sitter 这种 repomap 的方式？ 我总感觉 Cursor 的那种模式要高级一点， aider 适合自己的项目精确重构， Cursor 适应范围要广很多
   - (English: Research Cursor's prompts and see if RAG can be used to enhance the aider tree-sitter repomap method. I feel that Cursor's mode is more advanced; aider is suitable for precise refactoring of one's own projects, while Cursor has a much wider range of applications.)
6. 可以随时更改过去的某个prompt，然后重新发给LLM
   - (English: Allow modifying a past prompt at any time and resending it to the LLM.)
