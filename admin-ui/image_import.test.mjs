import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

import { DEFAULT_IMPORT_CONFIGURATION } from './src/folder_import.js';
import { ALLOWED_EXTENSIONS, validateFile } from './src/import_ui.js';


const IMAGE_EXTENSIONS = ['.bmp', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp'];


test('direct image formats are accepted by import fallbacks', () => {
    for (const extension of IMAGE_EXTENSIONS) {
        assert.equal(ALLOWED_EXTENSIONS.has(extension), true, extension);
        assert.equal(DEFAULT_IMPORT_CONFIGURATION.allowed_extensions.includes(extension), true, extension);
        assert.equal(
            validateFile({ name: `scan${extension}`, size: 1024, lastModified: 1 }).valid,
            true,
            extension,
        );
    }
});


test('preview import configuration advertises direct image formats', () => {
    const source = fs.readFileSync(new URL('./src/preview_mode.js', import.meta.url), 'utf8');
    for (const extension of IMAGE_EXTENSIONS) {
        assert.match(source, new RegExp(`['"]\\${extension}['"]`), extension);
    }
});
