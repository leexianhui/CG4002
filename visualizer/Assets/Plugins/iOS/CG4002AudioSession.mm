// Lets the game's sound play even when the iPhone's ring/silent switch is on.
// Unity's default audio session category ("ambient") is silenced by that switch; "playback" isn't.
// Called by GameAudio (FX/GameAudio.cs) at start-up and whenever the app comes back to the foreground.
#import <AVFoundation/AVFoundation.h>

extern "C" void CG4002_PlayEvenWhenSilent()
{
    NSError* error = nil;
    AVAudioSession* session = [AVAudioSession sharedInstance];
    [session setCategory:AVAudioSessionCategoryPlayback error:&error];
    if (error != nil)
        NSLog(@"[CG4002] Audio session category: %@", error);
    [session setActive:YES error:&error];
}
