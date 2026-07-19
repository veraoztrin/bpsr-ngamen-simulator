import mido
from mido import Message, MidiFile, MidiTrack

def create_scale():
    mid = MidiFile()
    track = MidiTrack()
    mid.tracks.append(track)
    
    # C Major Scale (MIDI 60 to 72)
    notes = [60, 62, 64, 65, 67, 69, 71, 72]
    
    for note in notes:
        track.append(Message('note_on', note=note, velocity=64, time=0))
        track.append(Message('note_off', note=note, velocity=64, time=480)) # 1 beat at 120bpm
        
    mid.save('test_1_c_major_scale.mid')

def create_octave_jumps():
    mid = MidiFile()
    track = MidiTrack()
    mid.tracks.append(track)
    
    # C3, C4, C5, C6 (MIDI 48, 60, 72, 84)
    notes = [48, 60, 72, 84]
    
    for note in notes:
        track.append(Message('note_on', note=note, velocity=64, time=0))
        track.append(Message('note_off', note=note, velocity=64, time=480))
        
    mid.save('test_2_octave_jumps.mid')

def create_chords():
    mid = MidiFile()
    track = MidiTrack()
    mid.tracks.append(track)
    
    # C Major Chord C4 (60, 64, 67)
    track.append(Message('note_on', note=60, velocity=64, time=0))
    track.append(Message('note_on', note=64, velocity=64, time=0))
    track.append(Message('note_on', note=67, velocity=64, time=0))
    
    track.append(Message('note_off', note=60, velocity=64, time=960))
    track.append(Message('note_off', note=64, velocity=64, time=0))
    track.append(Message('note_off', note=67, velocity=64, time=0))
    
    # C Major Chord C5 (72, 76, 79)
    track.append(Message('note_on', note=72, velocity=64, time=0))
    track.append(Message('note_on', note=76, velocity=64, time=0))
    track.append(Message('note_on', note=79, velocity=64, time=0))
    
    track.append(Message('note_off', note=72, velocity=64, time=960))
    track.append(Message('note_off', note=76, velocity=64, time=0))
    track.append(Message('note_off', note=79, velocity=64, time=0))

    mid.save('test_3_chords.mid')

if __name__ == "__main__":
    create_scale()
    create_octave_jumps()
    create_chords()
    print("Test MIDI files generated.")
