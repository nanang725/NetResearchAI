import type {
  PaperElement,
  TranslationDirection,
  TranslationRequest,
} from "@/types/paper";


export function orderedPageElements(elements: PaperElement[]): PaperElement[] {
  return [...elements].sort(
    (first, second) =>
      first.element_index - second.element_index || first.id - second.id,
  );
}


export function buildTranslationRequest(
  pageNumber: number,
  direction: TranslationDirection,
  elementId?: number,
): TranslationRequest {
  return {
    page_number: pageNumber,
    direction,
    ...(elementId === undefined ? {} : { element_id: elementId }),
  };
}


export function translationScopeKey(
  pageNumber: number,
  elementId?: number,
): string {
  return elementId === undefined
    ? `page:${pageNumber}`
    : `element:${pageNumber}:${elementId}`;
}
