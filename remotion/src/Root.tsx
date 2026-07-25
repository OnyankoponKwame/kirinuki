import { Composition } from "remotion";
import {
  ClipComposition,
  calculateMetadata,
  clipSchema,
} from "./ClipComposition";
import { StudioCompositions } from "./studioCompositions";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      {/* CLI rendering 用 */}
      <Composition
        id="ClipComposition"
        component={ClipComposition}
        schema={clipSchema}
        durationInFrames={300}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{
          videoSrc: "",
          startSec: 0,
          endSec: 10,
          vertical: true,
          verticalMode: "crop",
          cropX: 93,
          faceCamZoom: 2,
          faceCamY: 100,
          title: "",
          captions: [],
          captionFontSize: 96,
          captionFont: "mochiy",
        }}
        calculateMetadata={calculateMetadata}
      />

      {/* Studio preview clips — populated by "Studio で確認" button */}
      <StudioCompositions />
    </>
  );
};
