import assert from 'node:assert/strict';
import { copyTextToClipboard } from './src/copy_text.js';

// Node 环境没有 navigator.clipboard，因此会走降级路径。
// 我们构造一个最小 document 来验证降级行为：
//   1. 成功复制时 textarea 被移除
//   2. execCommand 抛异常时 textarea 仍被移除
//   3. execCommand 返回 false 时抛出中文错误

function makeDocument(execCommandImpl) {
    const nodes = [];
    return {
        body: {
            appendChild(node) {
                nodes.push(node);
                node.parentNode = this;
            },
            removeChild(node) {
                const idx = nodes.indexOf(node);
                if (idx !== -1) nodes.splice(idx, 1);
                node.parentNode = null;
            },
        },
        createElement(tag) {
            return {
                tag,
                value: '',
                style: {},
                attributes: {},
                parentNode: null,
                setAttribute(name, value) {
                    this.attributes[name] = value;
                },
                select() { /* no-op */ },
            };
        },
        execCommand: execCommandImpl,
        _nodes: nodes,
    };
}

const originalDocument = global.document;
const originalNavigator = global.navigator;

function setGlobal(name, value) {
    Object.defineProperty(global, name, {
        value,
        configurable: true,
        writable: true,
    });
}

async function run() {
    setGlobal('navigator', { clipboard: null });

    // 1. 成功复制
    {
        const doc = makeDocument(() => true);
        setGlobal('document', doc);
        await copyTextToClipboard('hello world');
        assert.equal(doc._nodes.length, 0, 'success: textarea removed');
    }

    // 2. execCommand 抛异常 -> textarea 仍被移除，且抛出中文错误
    {
        const doc = makeDocument(() => { throw new Error('boom'); });
        setGlobal('document', doc);
        try {
            await copyTextToClipboard('should fail');
            assert.fail('expected copy to throw');
        } catch (e) {
            assert.ok(e.message.includes('复制失败'), 'throws Chinese error on execCommand exception');
        }
        assert.equal(doc._nodes.length, 0, 'exception: textarea removed in finally');
    }

    // 3. execCommand 返回 false -> 抛出中文错误
    {
        const doc = makeDocument(() => false);
        setGlobal('document', doc);
        try {
            await copyTextToClipboard('should fail');
            assert.fail('expected copy to throw');
        } catch (e) {
            assert.equal(e.message, '复制失败，请手动选择内容', 'throws Chinese error when execCommand returns false');
        }
        assert.equal(doc._nodes.length, 0, 'false: textarea removed');
    }

    // 4. 空内容 -> 抛出“暂无可复制内容”
    {
        const doc = makeDocument(() => true);
        setGlobal('document', doc);
        try {
            await copyTextToClipboard('   ');
            assert.fail('expected empty copy to throw');
        } catch (e) {
            assert.equal(e.message, '暂无可复制内容', 'throws Chinese error for empty content');
        }
    }

    // 5. 复制内容写入 textarea.value
    {
        const doc = makeDocument(() => true);
        let capturedValue = null;
        const origAppend = doc.body.appendChild.bind(doc.body);
        doc.body.appendChild = function (node) {
            capturedValue = node.value;
            return origAppend(node);
        };
        setGlobal('document', doc);
        await copyTextToClipboard('expected value');
        assert.equal(capturedValue, 'expected value', 'writes text into textarea');
        assert.equal(doc._nodes.length, 0, 'textarea removed after copy');
    }

    setGlobal('document', originalDocument);
    setGlobal('navigator', originalNavigator);

    console.log('copy answer behavior test passed');
}

run().catch((e) => {
    setGlobal('document', originalDocument);
    setGlobal('navigator', originalNavigator);
    throw e;
});
