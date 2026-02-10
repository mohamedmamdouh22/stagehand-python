"""Observe handler for performing observations of page elements using LLMs."""

from typing import Any

from stagehand.a11y.utils import get_accessibility_tree, get_xpath_by_resolved_object_id
from stagehand.llm.inference import observe as observe_inference
from stagehand.metrics import StagehandFunctionName  # Changed import location
from stagehand.schemas import ObserveOptions, ObserveResult
from stagehand.utils import draw_observe_overlay


class ObserveHandler:
    """Handler for processing observe operations locally."""

    def __init__(
        self, stagehand_page, stagehand_client, user_provided_instructions=None
    ):
        """
        Initialize the ObserveHandler.

        Args:
            stagehand_page: StagehandPage instance
            stagehand_client: Stagehand client instance
            user_provided_instructions: Optional custom system instructions
        """
        self.stagehand_page = stagehand_page
        self.stagehand = stagehand_client
        self.logger = stagehand_client.logger
        self.user_provided_instructions = user_provided_instructions

    # TODO: better kwargs
    async def observe(
        self,
        options: ObserveOptions,
        from_act: bool = False,
    ) -> list[ObserveResult]:
        """
        Execute an observation operation locally.

        Args:
            options: ObserveOptions containing the instruction and other parameters

        Returns:
            list of ObserveResult instances
        """
        instruction = options.instruction
        if not instruction:
            instruction = (
                "Find elements that can be used for any future actions in the page. "
                "These may be navigation links, related pages, section/subsection links, "
                "buttons, or other interactive elements. Be comprehensive: if there are "
                "multiple elements that may be relevant for future actions, return all of them."
            )

        if not from_act:
            self.logger.info(
                f"Starting observation for task: '{instruction}'",
                category="observe",
            )

        # Start inference timer if available
        if hasattr(self.stagehand, "start_inference_timer"):
            self.stagehand.start_inference_timer()

        # Get DOM representation
        output_string = ""
        iframes = []

        await self.stagehand_page._wait_for_settled_dom()
        # Get accessibility tree data using our utility function
        self.logger.info("Getting accessibility tree data")
        include_iframes = options.iframes if options.iframes else False
        tree = await get_accessibility_tree(self.stagehand_page, self.logger, include_iframes=include_iframes)
        output_string = tree["simplified"]
        iframes = tree.get("iframes", [])

        # use inference to call the llm
        observation_response = await observe_inference(
            instruction=instruction,
            tree_elements=output_string,
            llm_client=self.stagehand.llm,
            user_provided_instructions=self.user_provided_instructions,
            logger=self.logger,
            log_inference_to_file=False,  # TODO: Implement logging to file if needed
            from_act=from_act,
        )

        # Extract metrics from response
        prompt_tokens = observation_response.get("prompt_tokens", 0)
        completion_tokens = observation_response.get("completion_tokens", 0)
        inference_time_ms = observation_response.get("inference_time_ms", 0)

        # Update metrics directly using the Stagehand client
        function_name = (
            StagehandFunctionName.ACT if from_act else StagehandFunctionName.OBSERVE
        )
        self.stagehand.update_metrics(
            function_name, prompt_tokens, completion_tokens, inference_time_ms
        )

        # Add iframes to the response if any (but only if iframes param is not enabled)
        # When iframes=True, the iframe content is already in the tree, so we don't need standalone iframe elements
        elements = observation_response.get("elements", [])
        if not include_iframes:
            for iframe in iframes:
                elements.append(
                    {
                        "element_id": int(iframe.get("nodeId", 0)),
                        "description": "an iframe",
                        "method": "not-supported",
                        "arguments": [],
                    }
                )

        # Create flat map of tree nodes for quick lookup of iframe metadata
        tree_node_map = self._flatten_tree_to_map(tree.get("tree", []))

        # Generate selectors for all elements
        elements_with_selectors = await self._add_selectors_to_elements(elements, tree_node_map)

        self.logger.debug(
            "Found elements", auxiliary={"elements": elements_with_selectors}
        )

        # Draw overlay if requested
        if options.draw_overlay:
            await draw_observe_overlay(
                page=self.stagehand_page,
                elements=[el.model_dump() for el in elements_with_selectors],
            )

        # Return the list of results without trying to attach _llm_response
        return elements_with_selectors

    def _flatten_tree_to_map(self, tree_nodes: list[dict]) -> dict[int, dict]:
        """
        Flatten tree structure into a map of backendDOMNodeId -> node for quick lookup.

        Args:
            tree_nodes: List of tree nodes from get_accessibility_tree

        Returns:
            Dictionary mapping backendDOMNodeId to node data
        """
        node_map = {}

        def traverse(nodes):
            for node in nodes:
                # Use backendDOMNodeId as key (this matches element_id from LLM)
                backend_node_id = node.get("backendDOMNodeId")
                if backend_node_id:
                    node_map[int(backend_node_id)] = node
                # Recursively traverse children
                if "children" in node and node["children"]:
                    traverse(node["children"])

        traverse(tree_nodes)
        return node_map

    async def _add_selectors_to_elements(
        self,
        elements: list[dict[str, Any]],
        tree_node_map: dict[int, dict] = None,
    ) -> list[ObserveResult]:
        """
        Add selectors to elements based on their element IDs.

        Args:
            elements: list of elements from LLM response
            tree_node_map: Optional map of nodeId -> node for iframe metadata lookup

        Returns:
            list of elements with selectors added (xpaths)
        """
        if tree_node_map is None:
            tree_node_map = {}

        result = []

        for element in elements:
            element_id = element.get("element_id")
            rest = {k: v for k, v in element.items() if k != "element_id"}

            # Generate xpath for element using CDP
            self.logger.info(
                "Getting xpath for element",
                auxiliary={"elementId": str(element_id)},
            )

            args = {"backendNodeId": element_id}
            response = await self.stagehand_page.send_cdp("DOM.resolveNode", args)
            object_id = response.get("object", {}).get("objectId")

            if not object_id:
                self.logger.info(
                    f"Invalid object ID returned for element: {element_id}"
                )
                continue

            # Use our utility function to get the XPath
            cdp_client = await self.stagehand_page.get_cdp_client()
            xpath = await get_xpath_by_resolved_object_id(cdp_client, object_id)

            if not xpath:
                self.logger.info(f"Empty xpath returned for element: {element_id}")
                continue

            # Check if this element came from inside an iframe
            node_data = tree_node_map.get(element_id)
            if node_data and "_iframe_xpath" in node_data:
                # Combine iframe xpath with element xpath
                iframe_xpath = node_data["_iframe_xpath"]
                combined_xpath = f"{iframe_xpath}{xpath}"
                result.append(ObserveResult(**{**rest, "selector": f"xpath={combined_xpath}"}))
            else:
                # Regular element (not inside iframe)
                result.append(ObserveResult(**{**rest, "selector": f"xpath={xpath}"}))

        return result
