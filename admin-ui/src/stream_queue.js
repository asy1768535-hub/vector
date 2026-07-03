// 流式文本渲染队列（纯函数，与 Vue/RAF 解耦，可供 Chat.js 和测试使用）
//
// 约定：
//   enqueue(t)  — 累积 delta 片段
//   flush()     — 返回并清空所有已入队片段（拼接为单个字符串）
//   cleanup()   — 清空队列（丢弃未渲染内容），返回丢弃的片段数量
//   isEmpty()   — 队列是否为空

export function createStreamQueue() {
    let queue = [];

    return {
        enqueue(text) {
            if (typeof text === 'string' && text.length) {
                queue.push(text);
            }
        },
        flush() {
            if (!queue.length) return '';
            const text = queue.join('');
            queue = [];
            return text;
        },
        cleanup() {
            const dropped = queue.length;
            queue = [];
            return dropped;
        },
        isEmpty() {
            return queue.length === 0;
        },
    };
}
