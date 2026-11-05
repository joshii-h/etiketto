# Design and print

## Content

Elements are placed one after another along the tape. Order them with
[[ui:Move up]] and [[ui:Move down]].

![Editing a text element](media/expand.png#only-light)
![Editing a text element](media/expand-dark.png#only-dark)

- **Text**: any installed font, colour, several lines. [[ui:Size]] 0 fills
  the tape height, otherwise fixed in pt.
- **Image**: photos and graphics; drag and drop works too.
- **Code**: QR code, Code 128 or EAN-13.

## Label

- [[ui:Automatic length]]: the label is as long as its content. Off: fixed
  [[ui:Length]], content aligned and scaled down if needed.
- [[ui:Spacing]]: space at the ends and between elements.
- [[ui:Edge spacing]]: extra space along the tape edges.
- [[ui:Background colour]], [[ui:Background image]]: printed edge to edge.
- [[ui:Border]]: width, 0 = none; always inside the safety margin.
- [[ui:Units]]: millimetres or inches, by default from the system language.

[[ui:Safety margin]] (printer setting, default 1.5 mm): content and border
keep this distance from the tape edges. The printer loses about 0.5 mm per
side, and long labels can run up to 1 mm askew. If text is cut off at the
edge, increase it.

## Print

![Print dialog](media/print.png#only-light)
![Print dialog](media/print-dark.png#only-dark)

- [[ui:Copies]]: number of labels.
- [[ui:Cut]]: [[ui:Full cut]] separates the labels. [[ui:Half cut]] cuts
  through the label, the backing holds them together. [[ui:No cut]]: the
  printer waits, swipe its panel to cut.
- [[ui:Colours]]: [[ui:Vivid]] for the strongest colours, [[ui:Normal]]
  (Brother: Standard) for softer ones, black and white (400 lpi) for text and
  codes.
- Colour correction: brightness and colour balance (red, green, blue), for
  this print only, not stored in the printer.

[[ui:Export PNG…]] saves the label as image.
