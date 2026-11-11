# Introducing ArsenalOS™: Anduril’s Digital Manufacturing Backbone

Introducing ArsenalOS™: Anduril’s Digital Manufacturing Backbone

Walk the floor of many defense factories and you'll find the most advanced hardware in the world being tracked the oldest way possible: on pen and paper, in spreadsheets passed between teams, in subcomponent catalogs living in one engineer's head or one binder on one bench. The way information moves around a defense factory is still, fundamentally, analog.

Running a factory on analog is slow, expensive, and error prone. When a design cannot move cleanly from the engineer who drew it to the technician who builds it, when it has to be re-keyed, re-drawn, and re-interpreted across disconnected systems, every one of those handoffs costs time.

The reason most factories run this way is structural. The legacy industrial base sits atop decades of accumulated infrastructure: incompatible machines, manual processes, and supply chains assembled program by program, none of them designed to communicate with the others, none of which Anduril inherited. Starting from first principles meant the factory and the software beneath it could be designed as one system, rather than assembled from parts never meant to work together.

This is why we’re building ArsenalOS, the software at the core of our industrial operations. The platform connects every stage of production, linking an engineer's initial design to the technician's hands on the floor within a single integrated system. Where the legacy approach fragments production across a separate tool for every step, the ArsenalOS platform governs the whole of it, from blueprint to battlefield.

Envisioning the Build Chain

Modern combat runs on a sequence the military calls the Kill Chain: Find, Fix, Track, Target, Engage, Assess. A target is detected, located with enough fidelity to act on, tracked as it moves, paired to a weapon, struck, and assessed. The Kill Chain is the primary building block for how the military thinks about fighting the wars of today and tomorrow, and closing it quickly is the whole objective.

Manufacturing has a parallel sequence we call the Build Chain: Define, Design, Source, Assemble, Test, Deploy. An engineering team defines a capability gap and the requirements to close it. They design a buildable solution, and sourcing validates the suppliers, materials, and lead times the design depends on. A manufacturing team assembles and integrates the hardware, tests it against spec, and deploys it to the field, where its performance feeds back into the next revision. Each stage maps onto the Kill Chain, Define to Find, Design to Fix, on through Deploy to Assess, but the resemblance ends at one decisive point. Where the Kill Chain is built to close as fast as possible, the Build Chain runs the other way: you keep the loop open as long as the work demands, and close it only once the design has earned it.

In a traditional defense factory, the loop is closed too early and reopened too slowly. Thousands of people build from printed 2D drawings, told the day's work at a standup or off a whiteboard. Hundreds inspect by hand, often with pen and paper. The hardware they assemble was locked by design engineers months or years earlier, after long cycles of reviews and decision gates. When the battlefield reveals that something needs to change, the path back to a revised version runs through manual redrawing, manually rewritten work instructions, and a fresh round of approvals. Every one of those steps carries a cost, and together they make changing a locked design enormously expensive.

ArsenalOS is built to keep that cost low, so a design can stay loose while a team is still learning, lock when the product reaches full-rate production, and reopen when the field forces a change. Three things make moving between those states safe rather than chaotic. A design change propagates automatically to every affected work instruction, rather than being redrawn by hand. The system tracks which units were built to which revision, so when a sensor swaps in or a part changes, you know exactly what is affected and what needs the retrofit. And it keeps the floor from building to a stale version, which is where a reopened design most often turns into scrapped work. Work instructions that were historically manual are now generated automatically, removing one of the slowest and most error-prone steps in the chain.


ArsenalOS and Its Architecture

Underneath, the ArsenalOS platform is a system of systems, combining Forge, our homegrown platform for manufacturing execution, maintenance, and quality, with commercial third-party systems, so every stage from design through field sustainment runs on the best available tool inside one connected environment. For factories running on analog, each stage of the Build Chain is siloed, and progress in one rarely moves at the pace of another. Consolidating the chain into a single system is what lets a battlefield-driven change flow back through design, sourcing, and the factory floor as one connected move, rather than stalling at each handoff.

Then those instructions are sent to the factory floor at machine speed. Software quickly identifies the tools needed to build the capability, automatically assesses inventory for components and materials, and provides realtime digitized directions for how to assemble it with precision. As the assembly process occurs — in production bay after production bay, embedded in small team after small team — that same software measures and monitors technical progress in real time across the enterprise. It records each weld or bolt tightened, who did it and when it happened, with every test and inspection result tagged and recorded. Once the hardware is delivered, the software can predictively assess the need for future updates and sustainment, marshal the necessary resources to do so, and ensure each of these operations are stored in a searchable way for each discrete component.

The result: billions in capital saved over time. Customers with the highest degree of hardware success assurance. And an iterative, adaptable model that can respond to realtime changes in production needs, order modifications, or new sources of supply.

By many measures, our ArsenalOS software is still getting started as we perpetually evolve the technology to power all parts of our production ecosystem. Building a layer like this is hard even for software-native companies, and close to prohibitive for ones retrofitting it onto decades of existing process. The advantage of building the Arsenal from the ground up is exactly here, because we get to design how the factory connects before anything is set in place.



The Battle of Production Starts Here

The advantages we accrue inside our manufacturing processes won’t stay there. Modern war increasingly isn’t just about armies coming into contact on the battlefield. Factories are fighting factories, hundreds of miles behind the frontlines. Weapons stocks determine deterrence. All signs indicate that the future of war will rely even more on production, resilience, and the orchestration of complex industrial operations.

In this environment, two tasks above all else will be the difference between victory and defeat: closing the Kill Chain and winning the Build Chain. With software-centric weapons systems that work first and fastest, the U.S. will be capable of the former. With software-centric industrial systems like ArsenalOS, the U.S. will create superior factories of the future and ensure we achieve the latter. By wielding its growing suite of battlefield and industrial technologies together, Anduril aspires to be responsible for success in both.

Pentagon planners want American industry on a “wartime footing.” Our adversaries and their war machines are only accelerating their own military production. Anduril’s ArsenalOS manufacturing software — and the Arsenal series of Anduril factories — is how we’ll respond.