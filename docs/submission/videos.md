# Original recordings

Both supplied files are distributed unchanged as [GitHub release assets](https://github.com/Tushar-kalsi-virtualfusion/cozmo-library-claim-agent/releases/tag/assessment-2026-10-20).
They were not stitched, sped up, re-encoded or given an added soundtrack during
submission preparation. These checks establish the identity of the supplied
files, not their editing history before they were supplied.

| Recording | Duration | Video | Audio tracks | Scope |
|---|---:|---|---:|---|
| `ScreenRecording_10-20-2026 01-20-26_1.mov` | 169.968333 s | 1206 x 2622, approximately 59.83 fps | 0 | Start, live camera/LiDAR preview, provisional outlines and supported object dimensions; ends during capture |
| `ScreenRecording_10-20-2026 08-40-49_1.MP4` | 47.408345 s | 1206 x 2622, approximately 59.04 fps | 1 | Later saved inventory: 3 book records and 76 non-book records, retained offers and partial processing |

Metadata was read with AVFoundation. Sampled video frames were visually reviewed.
An audio track exists in the second file; that alone does not establish audible
assistant speech or a complete voice interaction. The first recording has no
audio track. Neither file shows the required single uninterrupted, audible full
sweep through final packet.

The exported packet is the post-repair saved state associated with the second
recording. The first recording predates the identity repair, so counts visible
there need not match the exported packet.

## File integrity

| File | Exact bytes | SHA-256 |
|---|---:|---|
| Main MOV | 320499887 | `78a92ff416d231fc8c1ec117e13aacc67db1275e523c9e989f72950e8c4eea64` |
| Inventory MP4 | 88528008 | `f2b78c6262668d1dda6a305be132552bcf2a004ae15bfe8fef1e5a65b6df10c8` |

Download the release asset, then run `shasum -a 256` on it to compare the digest.
The originals are release downloads rather than Git blobs because of their size.
