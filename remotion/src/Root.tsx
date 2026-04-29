import { Composition } from "remotion";
import {
  ClipComposition,
  calculateMetadata,
  clipSchema,
  defaultClipProps,
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
        defaultProps={defaultClipProps}
        calculateMetadata={calculateMetadata}
      />

      {/* Studio preview clips — populated by "Studio で確認" button */}
      <StudioCompositions />
    </>
  );
};
