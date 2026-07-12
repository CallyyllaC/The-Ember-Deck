# Build story

[← Project overview](../README.md) · [Hardware and I/O →](HARDWARE.md)

This is a record of how the Ember Deck was built, not a reliable documentry on how another donor unit will come apart, survive the process or go back together in quite the same way.

The useful part is the order of decisions:

1. Keep the original controls that are worth saving.
2. Work out the largest physical constraints first.
3. Test each subsystem on its own.
4. Only make the wiring permanent once everything has found its final home.

## The donor

The Ember Deck began as an old Hitachi TV/radio/cassette unit.

![The donor TV and radio](../Images/TVRadio.jpg)

Again, please do not destroy working vintage equipmentm, there is more than enough broken hardware in the world already.

The unit also contained CRT circuitry. Do not poke it, prod it or experimentally discharge anything you do not understand. CRTs and their capacitors can retain dangerous voltages after the set has been unplugged.

If you cannot identify and discharge the high-voltage section safely, leave it alone and get help from someone who can.

Before removing anything, photograph everything:

- circuit boards
- connectors
- brackets
- tuning strings
- switch mechanisms
- cable routes
- anything that looks like it only fits one way

Vintage tuning mechanisms are especially good at looking simple until the string comes off.

![The opened donor case](../Images/OpenCase.jpg)

## Strip and salvage

The original boards, paper speaker and obsolete electronics were removed while the useful mechanical parts were kept.

The switches, selector mechanisms, potentiometers and tape keys were worth saving because modern replacements rarely feel the same. The movement and resistance of the original controls are a large part of what makes the finished Deck feel like a conversion rather than a Raspberry Pi in a novelty box.

![The original tape mechanism](../Images/TapeDeck.jpg)

![The front-panel controls](../Images/FrontButtons.jpg)

Not everything survived.

Old plastic becomes brittle. Old rubber turns to dust, or occasionally back into oil (it got everywhere). One original potentiometer disintegrated during removal, and some of the selector hardware was too fragile to justify putting back into service.

In the end, I only reused one of the original potentiometers. The others either broke or were unsuitable because of their physical size or electrical output.

I decided to preserve the parts that still worked properly and replace the ones that did not. Building the entire machine around a failing component for the sake of purity would have been more annoying than practical.

Electronic waste and CRT parts should be taken to a suitable recycling facility. Do not put them in normal household waste unless your local council specifically accepts them.

## Clean and prepare the enclosure

Once the original electronics were removed, the case needed a proper clean and quite a lot of internal surgery.

The plastic shell was washed and the rust marks removed, then the unwanted internal moulding was cut away to make room for the new hardware, wiring and subwoofer enclosure.

![Empty upper case](../Images/TopCase_Empty.jpg)

![Empty lower case](../Images/BottomCase_Empty.jpg)

Use cleaning products that suit the material. Contact cleaner is useful around electrical parts, and a proper plastic cleaner will also last longer than furniture polish, which was all I had available at the time and therefore what it received.

This stage is also where it becomes obvious how little usable space an old radio actually contains once you try to fit modern off the shelf electronics inside it. Empty-looking space quickly disappears when screens, speaker boxes, cable bends and service access are included.

## Speakers

The original unit was mono. The final Deck uses a 2.1 amplifier, two external full-range speakers and an internal subwoofer mounted behind the original front grille. Also, don't measure by the speaker grill shape, the visual speaker size was an obvious markeeting ploy, the real internal speaker mount and grill holes covered a conciderably smaller area.

For the external speakers I actually used two electric terminal boxes, and connected them to the main body using two metal door cable conduits.

The subwoofer enclosure was one of the first major parts to be positioned because it controls so much of the final layout:

- internal space
- airflow
- amplifier position
- cable routes
- access to the controls
- what can still be reached once the case is closed

![Inside the speaker enclosure](../Images/Speaker_Internals.jpg)

The enclosure was made from thick MDF, layered with acoustic foam and sealed properly. The cuts were not perfect because I am not a woodworker, and the finished box would not win anything at a furniture show.

But, functionally it is *sound*.

![Sealed speaker enclosure](../Images/Speaker_Sealed.jpg)

What matters is that it is rigid and airtight. The difference between the unsealed and sealed versions was far greater than the untidy appearance suggested. Hot glue, PVA and decorators' caulk are not glamorous, but still beat sound waves.

## Packaging the electronics

The first complete internal layout proved that the hardware worked.

It also proved that "working" and "finished" are very different things.

![Early internal wiring](../Images/The%20Rats%20Nest.jpg)

At this stage the Deck had power, audio, displays, controls and LEDs, but the internal wiring was difficult to follow and even worse to service. It was the usual prototype problem: every new subsystem had been added wherever it could be made to fit at the time.

The final pass was less about adding features and more about making the existing machine survivable.

Power distribution was grouped properly, cable runs were shortened and wrapped, airflow was kept clear, and the connections most likely to need attention were left reachable.

![Final internal wiring](../Images/The%20Rats%20Nest%20Tamed.jpg)

The main lesson here is to avoid committing to neat cable lengths too early.

Do not build the final harness until the screens, speaker enclosure, PSU, amplifier, powered hub, controls and converters are all sitting in realistic positions.

The case also had to come apart several times during final assembly because connectors unseated, buttons stopped behaving and USB devices developed the usual belief that physical contact was optional. That was irritating, but it was still better than sealing everything permanently and discovering the problem afterwards.

## Finished result

![Final left view](../Images/Left%20Final.jpg)

![Final top view](../Images/Top%20Final.jpg)

![Final top-right view](../Images/Top%20Right%20Final.jpg)

The finished Deck keeps the donor's knobs, tuning scale, tape keys and overall character.

That balance mattered throughout the build. The aim was never to make the conversion invisible, but about finding a balance between original aesthetics and end user usability with modern applications.

The earlier photographs are still kept in the repository as part of the build record. The three photographs above show the final V1 state.

After nine months of on-and-off work, repeated disassembly and a quite unreasonable amount of debugging, it is finished.

Not flawless. Finished.

See the [media gallery](MEDIA.md) for demonstration videos of the displays, controls and visualiser.
