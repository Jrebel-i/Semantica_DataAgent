import type { Messages } from "../i18n";

export const ATTACHMENT_LIMITS = {
  maxFiles: 10,
  maxFileBytes: 20 * 1024 * 1024,
  maxTotalBytes: 50 * 1024 * 1024,
};

const IMAGE_EXTENSIONS = [
  ".avif",
  ".bmp",
  ".gif",
  ".heic",
  ".heif",
  ".jpeg",
  ".jpg",
  ".png",
  ".tif",
  ".tiff",
  ".webp",
];

let nextId = 0;

export type PendingAttachment = {
  id: string;
  file: File;
  isImage: boolean;
  previewUrl?: string;
};

export function isImageFile(file: File): boolean {
  if (file.type.toLowerCase().startsWith("image/")) return true;
  const name = file.name.toLowerCase();
  return IMAGE_EXTENSIONS.some((suffix) => name.endsWith(suffix));
}

export function formatAttachmentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function attachmentSizeError(
  file: File,
  messages: Messages,
): string | null {
  if (file.size > ATTACHMENT_LIMITS.maxFileBytes) {
    return messages.attachmentLimitSize.replace("{name}", file.name);
  }
  return null;
}

export function validateAttachments(
  files: File[],
  existing: PendingAttachment[],
  messages: Messages,
  visionEnabled: boolean,
): { items: PendingAttachment[]; error: string } {
  if (existing.length + files.length > ATTACHMENT_LIMITS.maxFiles) {
    return { items: [], error: messages.attachmentLimitCount };
  }
  const total =
    existing.reduce((sum, item) => sum + item.file.size, 0) +
    files.reduce((sum, file) => sum + file.size, 0);
  for (const file of files) {
    const error = attachmentSizeError(file, messages);
    if (error) return { items: [], error };
  }
  if (total > ATTACHMENT_LIMITS.maxTotalBytes) {
    return { items: [], error: messages.attachmentLimitTotal };
  }
  if (files.some(isImageFile) && !visionEnabled) {
    return { items: [], error: messages.attachmentVisionUnsupported };
  }
  const items = files.map((file) => {
    const isImage = isImageFile(file);
    const previewUrl =
      isImage && typeof URL.createObjectURL === "function"
        ? URL.createObjectURL(file)
        : undefined;
    return { id: `attachment-${++nextId}`, file, isImage, previewUrl };
  });
  return { items, error: "" };
}

export function releaseAttachment(attachment: PendingAttachment): void {
  if (attachment.previewUrl && typeof URL.revokeObjectURL === "function") {
    URL.revokeObjectURL(attachment.previewUrl);
  }
}
