export default {
    props: { guide: { type: Object, required: true } },
    template: `
    <article class="api-access-guide">
        <p class="api-access-guide-intro">{{ guide.intro }}</p>
        <p class="api-access-guide-links">
            <a v-for="link in guide.links" :key="link.url" :href="link.url" target="_blank" rel="noopener noreferrer">{{ link.label }}</a>
        </p>
        <details v-for="(section, index) in guide.sections" :key="section.title" open class="api-access-guide-section">
            <summary>{{ index + 1 }}. {{ section.title }}</summary>
            <div class="api-access-guide-section-body">
                <p v-for="paragraph in section.paragraphs || []" :key="paragraph">{{ paragraph }}</p>
                <dl v-if="section.rows" class="api-access-guide-fields">
                    <div v-for="row in section.rows" :key="row[0]"><dt><code>{{ row[0] }}</code></dt><dd>{{ row[1] }}</dd></div>
                </dl>
                <ol v-if="section.steps"><li v-for="step in section.steps" :key="step">{{ step }}</li></ol>
                <pre v-if="section.code" class="api-keys-doc-code">{{ section.code }}</pre>
                <section v-for="operation in section.operations || []" :key="operation.name" class="api-access-guide-operation">
                    <h4><code>{{ operation.name }}</code></h4>
                    <p>{{ operation.purpose }}</p>
                    <p class="api-access-guide-caption">参数 / 请求示例</p>
                    <pre class="api-keys-doc-code">{{ operation.example }}</pre>
                    <p><strong>结果：</strong>{{ operation.result }}</p>
                </section>
            </div>
        </details>
    </article>`,
};
