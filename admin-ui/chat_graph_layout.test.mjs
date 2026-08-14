import assert from 'node:assert/strict';
import test from 'node:test';

import { layoutCitationGraph } from './src/chat_graph_layout.js';

const seed = { id: 'seed', depth: 0, canonical_name: '专项施工方案审核', entity_type: { key: 'process', label: '流程' } };
const inbound = { id: 'person', depth: 1, canonical_name: '总监理工程师', entity_type: { key: 'person', label: '人员' } };
const outbound = { id: 'result', depth: 1, canonical_name: '验收', entity_type: { key: 'process', label: '流程' } };
const graph = {
    nodes: [outbound, seed, inbound],
    relations: [
        { id: 'approves', source_entity_id: 'person', target_entity_id: 'seed', relation_type: { label: '审核', direction: 'directed' } },
        { id: 'leads', source_entity_id: 'seed', target_entity_id: 'result', relation_type: { label: '前置于', direction: 'directed' } },
    ],
};

test('citation layout seeds a center-and-neighbor graph for force simulation', () => {
    const layout = layoutCitationGraph(graph, 900, 500, 'seed');
    const byId = new Map(layout.nodes.map((node) => [node.id, node]));
    assert.deepEqual({ x: byId.get('seed').x, y: byId.get('seed').y }, { x: 450, y: 250 });
    assert.equal(byId.get('seed').role, 'focus');
    assert.equal(byId.get('seed').entityTypeLabel, '流程');
    assert.equal(byId.get('person').role, 'neighbor');
    assert.notDeepEqual(
        { x: byId.get('person').x, y: byId.get('person').y },
        { x: byId.get('result').x, y: byId.get('result').y },
    );
    assert.equal(layout.relations.every((relation) => relation.label.length > 0), true);
});

test('citation graph produces deterministic finite starting geometry', () => {
    const first = layoutCitationGraph(graph, 720, 420);
    const second = layoutCitationGraph(graph, 720, 420);
    assert.deepEqual(first, second);
    assert.ok(first.nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y)));
});

test('same-depth neighbors use varied radii instead of a perfect ring', () => {
    const neighbors = Array.from({ length: 12 }, (_, index) => ({
        ...inbound,
        id: `neighbor-${index}`,
        canonical_name: `相邻实体 ${index + 1}`,
    }));
    const denseGraph = {
        nodes: [seed, ...neighbors],
        relations: neighbors.map((item, index) => ({
            id: `edge-${index}`,
            source_entity_id: seed.id,
            target_entity_id: item.id,
            relation_type: { label: '关联', direction: 'directed' },
        })),
    };
    const result = layoutCitationGraph(denseGraph, 900, 600, seed.id);
    const focus = result.nodes.find((item) => item.role === 'focus');
    const radii = result.nodes
        .filter((item) => item.role === 'neighbor')
        .map((item) => Math.round(Math.hypot(item.x - focus.x, item.y - focus.y)));
    assert.ok(new Set(radii).size >= 6);
    assert.equal(new Set(result.nodes.map((item) => `${item.x}:${item.y}`)).size, result.nodes.length);
});

test('a requested node becomes the only center while all neighbors remain distinct', () => {
    const manySeeds = Array.from({ length: 10 }, (_, index) => ({
        ...seed,
        id: `seed-${index}`,
        canonical_name: `引用实体 ${index + 1}`,
    }));
    const layout = layoutCitationGraph({ nodes: manySeeds, relations: [] }, 900, 500, 'seed-4');
    const positions = new Set(layout.nodes.map((node) => `${node.x}:${node.y}`));
    assert.equal(positions.size, manySeeds.length);
    assert.equal(layout.nodes.filter((node) => node.role === 'focus').length, 1);
    assert.equal(layout.nodes.find((node) => node.role === 'focus').id, 'seed-4');
});

test('panorama groups real components without library nodes or synthetic edges', () => {
    const libraries = [
        { id: 'library-a', slug: 'a', name: '工程资料库' },
        { id: 'library-b', slug: 'b', name: '设备知识库' },
    ];
    const nodes = [
        { ...seed, id: 'a-1', library: libraries[0], depth: 1 },
        { ...inbound, id: 'a-2', library: libraries[0], depth: 1 },
        { ...outbound, id: 'b-1', library: libraries[1], depth: 1 },
    ];
    const relations = [{
        id: 'real-edge',
        source_entity_id: 'a-1',
        target_entity_id: 'a-2',
        relation_type: { label: '关联', direction: 'directed' },
    }];
    const layout = layoutCitationGraph({ panorama: true, nodes, relations }, 1000, 600);
    assert.equal(layout.nodes.some((node) => node.role === 'focus'), false);
    assert.deepEqual(layout.relations.map((relation) => relation.id), ['real-edge']);
    assert.equal(layout.nodes.some((node) => node.id.startsWith('library:')), false);
    const isolated = layout.nodes.find((node) => node.id === 'b-1');
    assert.equal(isolated.role, 'isolated');
    assert.ok(isolated.x > 720);
    const pair = ['a-1', 'a-2'].map((id) => layout.nodes.find((node) => node.id === id));
    assert.ok(Math.hypot(pair[0].x - pair[1].x, pair[0].y - pair[1].y) <= 70);
    assert.ok(pair.every((node) => node.role === 'pair'));
    assert.ok(Math.max(...pair.map((node) => node.x)) < isolated.x);
});
