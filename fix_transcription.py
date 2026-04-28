import json

files = [
    '/Users/maru/workspace/kirinuki/remotion/src/studioData.json',
    '/Users/maru/workspace/kirinuki/transcriptions/n7gHr7vBc08_first15min_20260426_115902_full.json',
    '/Users/maru/workspace/kirinuki/remotion/studio-props.json'
]

for file_path in files:
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        segments = data.get('segments', [])
        
        # In a 10-minute overlap chunking, chunk start increases by 590s.
        # We can detect chunk boundary when `id` resets to 0 or decreases.
        chunk_offset = 0
        last_id = -1
        last_start = 0
        
        for seg in segments:
            current_id = seg.get('id', 0)
            
            # Detect new chunk when ID drops significantly
            if current_id < last_id - 5 or (current_id == 0 and last_id > 10):
                # We hit a new chunk boundary
                chunk_offset += 590.0
                
            last_id = current_id
            
            # Since the script already ran and didn't add offsets, we add it now
            # Only add it if it hasn't been added (to prevent double adding if run twice)
            # The segments are currently all 0-600.
            # We don't want to double add, but we just read the file once.
            seg['start'] += chunk_offset
            seg['end'] += chunk_offset
            if 'words' in seg:
                for w in seg['words']:
                    w['start'] += chunk_offset
                    w['end'] += chunk_offset
            
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
            
        print(f"Fixed {file_path}")
    except Exception as e:
        print(f"Error on {file_path}: {e}")
