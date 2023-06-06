from __future__ import annotations
from datetime import datetime
import threading
from uuid import uuid4
from time import sleep
import json
from typing import List, Optional

from pydantic import ValidationError
from jsonschema import validate

import chromadb
import faiss
from langchain.vectorstores import FAISS
from langchain.docstore import InMemoryDocstore
from langchain.tools import StructuredTool
from langchain.tools.human.tool import HumanInputRun
from langchain.vectorstores.base import VectorStoreRetriever
from langchain.vectorstores import Chroma
from langchain.embeddings import OpenAIEmbeddings
from langchain.schema import (
    AIMessage,
    BaseMessage,
    Document,
    HumanMessage,
    SystemMessage,
)
from genworlds.agents.tree_agent.brains.event_filler_brain import EventFillerBrain

from genworlds.agents.yeager_autogpt.output_parser import (
    AutoGPTAction,
    AutoGPTOutputParser,
)
from genworlds.agents.yeager_autogpt.prompt_generator import FINISH_NAME
from genworlds.sockets.world_socket_client import WorldSocketClient
from genworlds.agents.yeager_autogpt.listening_antenna import ListeningAntenna
from genworlds.events.basic_events import (
    AgentGetsNearbyEntitiesEvent,
    AgentGetsObjectInfoEvent,
    AgentGetsAgentInfoEvent,
    AgentGivesObjectToAgentEvent,
    AgentSpeaksWithAgentEvent,
    EntityRequestWorldStateUpdateEvent,
)
from genworlds.utils.logging_factory import LoggingFactory

from genworlds.agents.tree_agent.brains.navigation_brain import (
    NavigationBrain,
)
from genworlds.agents.tree_agent.brains.podcast_brain import PodcastBrain
from genworlds.agents.tree_agent.memory_summarizers import MemorySummarizer

class TreeAgent:
    """Agent class for interacting with Auto-GPT."""

    world_spawned_id: str
    personality_db = None

    def __init__(
        self,
        ai_name: str,
        description: str,
        goals: List[str],
        openai_api_key: str,

        navigation_brain: NavigationBrain,
        execution_brains: dict,        
        action_brain_map: dict,
        interesting_events: set = {},
        feedback_tool: Optional[HumanInputRun] = None,
        additional_memories: Optional[List[VectorStoreRetriever]] = None,
        id: str = None,
        personality_db_path: str = None,
        personality_db_collection_name: str = None,
        websocket_url: str = "ws://127.0.0.1:7456/ws",
    ):
        # Its own properties
        self.id = id if id else str(uuid4())
        self.ai_name = ai_name
        self.description = description
        self.goals = goals
        self.interesting_events = interesting_events
        self.feedback_tool = feedback_tool
        self.additional_memories = additional_memories

        self.logger = LoggingFactory.get_logger(self.ai_name)

        self.world_socket_client = WorldSocketClient(
            process_event=None, url=websocket_url
        )

        self.listening_antenna = ListeningAntenna(
            self.interesting_events,
            agent_name=self.ai_name,
            agent_id=self.id,
            websocket_url=websocket_url,
        )

        # Agent actions
        self.actions = []

        # Brain properties
        self.memory_summarizer = MemorySummarizer(openai_api_key=openai_api_key)
        self.embeddings_model = OpenAIEmbeddings(openai_api_key=openai_api_key)
        embedding_size = 1536
        index = faiss.IndexFlatL2(embedding_size)
        vectorstore = FAISS(
            self.embeddings_model.embed_query, index, InMemoryDocstore({}), {}
        )
        self.memory = vectorstore.as_retriever()

        self.navigation_brain = navigation_brain
        self.execution_brains = execution_brains
        self.action_brain_map = action_brain_map

        self.full_message_history: List[BaseMessage] = []
        self.next_action_count = 0
        self.output_parser = AutoGPTOutputParser()
        self.feedback_tool = None  # HumanInputRun() if human_in_the_loop else None
        self.schemas_memory: Chroma
        self.plan: Optional[str] = None

        self.personality_db_path = personality_db_path
        if self.personality_db_path:
            client_settings = chromadb.config.Settings(
                chroma_db_impl="duckdb+parquet",
                persist_directory=self.personality_db_path,
                anonymized_telemetry=False,
            )

            self.personality_db = Chroma(
                collection_name=personality_db_collection_name,
                embedding_function=self.embeddings_model,
                client_settings=client_settings,
                persist_directory=self.personality_db_path,
            )

    def think(self):
        self.logger.info(f" The agent {self.ai_name} is thinking...")
        user_input = (
            "Determine which next command to use, "
            "and respond using the format specified above:"
        )
        # Get the initial world state
        self.agent_request_world_state_update_action()

        sleep(5)

        while True:
            agent_world_state = self.listening_antenna.get_agent_world_state()
            nearby_entities = self.listening_antenna.get_nearby_entities()

            if len(nearby_entities) > 0:
                nearby_entities_store = Chroma.from_texts(
                    list(map(json.dumps, nearby_entities)), self.embeddings_model
                )

                if self.plan:
                    useful_nearby_entities = nearby_entities_store.similarity_search(
                        self.plan
                    )
                else:
                    useful_nearby_entities = nearby_entities_store.similarity_search(
                        json.dumps(self.goals)
                    )

                useful_nearby_entities = list(
                    map(lambda d: json.loads(d.page_content), useful_nearby_entities)
                )
            else:
                useful_nearby_entities = []

            relevant_commands = {}
            for entity in useful_nearby_entities:
                entity_schemas = self.get_schemas()[entity["entity_class"]]

                for event_type, schema in entity_schemas.items():
                    description = schema["properties"]["description"]["default"]

                    args = {}
                    for property_name, property_details in schema["properties"].items():
                        if property_name not in [
                            "event_type",
                            "description",
                            "created_at",
                            "sender_id",
                            "summary",
                        ]:
                            args[property_name] = property_details

                    command = {
                        "title": f"{entity['entity_class']}:{event_type}",
                        "description": description,
                        "args": args,
                        "string_short": f"{entity['entity_class']}:{event_type} - {description}",
                        "string_full": f"\"{entity['entity_class']}:{event_type}\" - {description}, args json schema: {json.dumps(args)}",
                    }
                    relevant_commands[command["title"]] = command

            # Add world
            entity_class = "World"
            entity_schemas = self.get_schemas()[entity_class]

            for event_type, schema in entity_schemas.items():
                if event_type in self.listening_antenna.special_events:
                    continue

                description = schema["properties"]["description"]["default"]

                args = {}
                for property_name, property_details in schema["properties"].items():
                    if property_name not in [
                        "event_type",
                        "description",
                        "created_at",
                        "sender_id",
                        "summary",
                    ]:
                        args[property_name] = property_details

                command = {
                    "title": f"{entity_class}:{event_type}",
                    "description": description,
                    "args": args,
                    "string_short": f"{entity_class}:{event_type} - {description}",
                    "string_full": f'"{entity_class}:{event_type}" - {description}, args json schema: {json.dumps(args)}',
                }
                relevant_commands[command["title"]] = command

            # Send message to AI, get response
            navigation_plan = self.navigation_brain.run(
                {
                    "goals": self.goals,
                    "messages": self.full_message_history,
                    "memory": self.memory,
                    "personality_db": self.personality_db,
                    "nearby_entities": list(
                        filter(lambda e: (e["held_by"] != self.id), nearby_entities)
                    ),
                    "inventory": list(
                        filter(lambda e: (e["held_by"] == self.id), nearby_entities)
                    ),
                    "plan": self.plan,
                    "user_input": user_input,
                    "agent_world_state": agent_world_state,
                    "relevant_commands": map(
                        lambda c: c["string_short"], relevant_commands.values()
                    ),
                }
            )

            # Print Assistant thoughts
            self.logger.info(navigation_plan)
            self.full_message_history.append(AIMessage(content=str(navigation_plan)))

            # Parse response
            navigation_plan_parsed = json.loads(navigation_plan)
            self.plan = navigation_plan_parsed["plan"]

            selected_action = navigation_plan_parsed["next_action"]
            action_goal_description = navigation_plan_parsed["goal"]

            result = ""
            event_sent_summary = ""
            if selected_action == FINISH_NAME:
                return "FINISHED"
            elif selected_action == "Self:wait":
                self.logger.info(f"Waiting...")
                result += f"Waiting...\n"
            # TODO: tools?
            elif selected_action in relevant_commands:
                command = relevant_commands[selected_action]

                if selected_action in self.action_brain_map:
                    action_brains = self.action_brain_map[selected_action]
                else:
                    action_brains = self.action_brain_map["default"]

                previous_brain_outputs = [
                    f"Current goal: {action_goal_description}",
                ]
                for action_brain_name in action_brains:
                    action_brain = self.execution_brains[action_brain_name]

                    previous_brain_outputs.append(
                        action_brain.run(
                            {
                                "goals": self.goals,
                                "messages": self.full_message_history,
                                "memory": self.memory,
                                "personality_db": self.personality_db,
                                "nearby_entities": list(
                                    filter(
                                        lambda e: (e["held_by"] != self.id),
                                        nearby_entities,
                                    )
                                ),
                                "inventory": list(
                                    filter(
                                        lambda e: (e["held_by"] == self.id),
                                        nearby_entities,
                                    )
                                ),
                                "plan": self.plan,
                                "user_input": user_input,
                                "agent_world_state": agent_world_state,
                                "command_to_execute": command["string_full"],
                                "previous_brain_outputs": previous_brain_outputs,
                            }
                        )
                    )

                args = json.loads(previous_brain_outputs[-1])

                assert (
                    type(args) == dict
                ), f"Final action brain {action_brain} did not return a dict, returned {args} instead. The action brain map wasn't set up correctly."
                event_sent = self.execute_event_with_args(command["title"], args)
                event_sent_summary += "Event timestamp: " + event_sent["created_at"] + "\n"
                event_sent_summary += event_sent["sender_id"] + " sent "
                event_sent_summary += event_sent["event_type"] + " to "
                event_sent_summary += str(event_sent["target_id"]) + "\n"
                event_sent_summary += "And this is the summary of what happened: "+ str(event_sent["summary"]) + "\n"
                # result += self.execute_event_with_args(command["title"], args) + "\n"

            else:
                self.logger.info(f"Invalid command: {selected_action}")
                result += f"Error: {selected_action} is not recognized. \n"
                continue

            ## send result and assistant_reply to the socket
            self.logger.info(result)

            # If there are any relevant events in the world for this agent, add them to memory
            sleep(3)
            last_events = self.listening_antenna.get_last_events()
            memory_to_add = ""
            for event in last_events:
                memory_to_add += "Event timestamp: " + event["created_at"] + "\n"
                memory_to_add += event["sender_id"] + " sent "
                memory_to_add += event["event_type"] + " to "
                memory_to_add += str(event["target_id"]) + "\n"
                memory_to_add += "And this is the summary of what happened: "+ str(event["summary"]) + "\n"

            self.logger.debug(f"Adding to memory: {memory_to_add}")

            if self.feedback_tool is not None:
                feedback = f"\n{self.feedback_tool.run('Input: ')}"
                if feedback in {"q", "stop"}:
                    self.logger.info("EXITING")
                    return "EXITING"
                memory_to_add += feedback

            if memory_to_add != "":
                self.memory.add_documents([Document(page_content=memory_to_add)])
            self.full_message_history.append(SystemMessage(content=result))

    def get_agent_world_state(self):
        return self.listening_antenna.get_agent_world_state()

    def get_nearby_entities(self):
        return self.listening_antenna.get_nearby_entities()

    def get_schemas(self):
        return self.listening_antenna.get_schemas()

    def execute_event_with_args(self, name: str, args: dict):
        try:
            class_name = name.split(":")[0]
            event_type = name.split(":")[1]

            event = {
                "event_type": event_type,
                "sender_id": self.id,
                "created_at": datetime.now().isoformat(),
            }
            event.update(args)
            summary = self.memory_summarizer.summarize(json.dumps(event))
            event["summary"] = summary
            self.logger.debug(event)

            event_schema = self.get_schemas()[class_name][event_type]
            validate(event, event_schema)

            self.world_socket_client.send_message(json.dumps(event))
            return event
        except IndexError as e:
            return (
                f"Unknown command '{name}'. "
                f"Please refer to the 'COMMANDS' list for available "
                f"commands and only respond in the specified JSON format."
            )
        except ValidationError as e:
            return f"Validation Error in args: {str(e)}, args: {args}"
        except Exception as e:
            return f"Error: {str(e)}, {type(e).__name__}, args: {args}"

    def agent_gets_nearby_entities_action(self):
        agent_gets_nearby_entities_event = AgentGetsNearbyEntitiesEvent(
            created_at=datetime.now(),
            sender_id=self.id,
        )
        self.world_socket_client.send_message(agent_gets_nearby_entities_event.json())

    def agent_gets_object_info_action(
        self,
        target_id: str,
    ):
        agent_gets_object_info = AgentGetsObjectInfoEvent(
            created_at=datetime.now(),
            sender_id=self.id,
            target_id=target_id,
        )
        self.world_socket_client.send_message(agent_gets_object_info.json())

    def agent_gets_agent_info_action(
        self,
        target_id: str,
    ):
        agent_gets_agent_info = AgentGetsAgentInfoEvent(
            created_at=datetime.now(),
            sender_id=self.id,
            target_id=target_id,
        )
        self.world_socket_client.send_message(agent_gets_agent_info.json())

    def agent_interacts_with_object_action(
        self,
        created_interaction: str,
    ):
        self.world_socket_client.send_message(created_interaction)

    def agent_speaks_with_agent_action(
        self,
        target_id: str,
        message: str,
    ):
        agent_speaks_with_agent = AgentSpeaksWithAgentEvent(
            created_at=datetime.now(),
            sender_id=self.id,
            target_id=target_id,
            message=message,
        )
        self.world_socket_client.send_message(agent_speaks_with_agent.json())

    def agent_request_world_state_update_action(self):
        agent_request_world_state_update = EntityRequestWorldStateUpdateEvent(
            created_at=datetime.now(),
            sender_id=self.id,
            target_id=self.world_spawned_id,
        )
        self.world_socket_client.send_message(agent_request_world_state_update.json())

    def launch_threads(self):
        threading.Thread(
            target=self.listening_antenna.world_socket_client.websocket.run_forever,
            name=f"Agent {self.ai_name} Listening Thread",
            daemon=True,
        ).start()
        sleep(0.1)
        threading.Thread(
            target=self.world_socket_client.websocket.run_forever,
            name=f"Agent {self.ai_name} Speaking Thread",
            daemon=True,
        ).start()
        sleep(0.1)
        threading.Thread(
            target=self.think,
            name=f"Agent {self.ai_name} Thinking Thread",
            daemon=True,
        ).start()
        self.logger.info("Threads launched")